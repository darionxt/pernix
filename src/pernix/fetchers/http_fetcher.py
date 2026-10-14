from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from curl_cffi import CurlECode, CurlError, CurlHttpVersion
from curl_cffi.requests import exceptions as curl_errors

from pernix.cookies import merge_set_cookies, parse_cookie_header
from pernix.errors import (
    ConnectError,
    DecodeError,
    FetchError,
    ProxyError,
    ResponseTooLarge,
    Timeout,
    TLSError,
    TooManyRedirects,
)
from pernix.fetchers.curl_session_pool import CurlSession, CurlSessionPool, Transfer
from pernix.fetchers.http_settings import HTTPSettings, check_profile
from pernix.fetchers.streamed_response import StreamedResponse
from pernix.headers import Headers
from pernix.response import Redirect, Response
from pernix.settings import CoreSettings, ResponseSettings, Section

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

    from pernix.crawler import Crawler
    from pernix.request import Request

__all__ = ["HTTPFetcher"]


_HTTP_VERSIONS: dict[int, str] = {
    CurlHttpVersion.V1_0: "HTTP/1.0",
    CurlHttpVersion.V1_1: "HTTP/1.1",
    CurlHttpVersion.V2_0: "HTTP/2",
    CurlHttpVersion.V3: "HTTP/3",
}
"""What libcurl reports as the HTTP version of a transfer, by name."""

_ERROR_KINDS: tuple[tuple[type[curl_errors.RequestException], type[FetchError]], ...] = (
    (curl_errors.ProxyError, ProxyError),
    (curl_errors.SSLError, TLSError),  # before ConnectionError, its base
    (curl_errors.Timeout, Timeout),  # before ConnectionError, a base of ConnectTimeout
    (curl_errors.ConnectionError, ConnectError),
    (curl_errors.IncompleteRead, ConnectError),
    (curl_errors.TooManyRedirects, TooManyRedirects),
)
"""curl-cffi errors and the FetchErrors they become, most specific first."""

_ERROR_CODES: dict[int, type[FetchError]] = {
    CurlECode.BAD_CONTENT_ENCODING: DecodeError,
    CurlECode.FILESIZE_EXCEEDED: ResponseTooLarge,
    CurlECode.OPERATION_TIMEDOUT: Timeout,
    CurlECode.COULDNT_CONNECT: ConnectError,
    CurlECode.COULDNT_RESOLVE_HOST: ConnectError,
    CurlECode.COULDNT_RESOLVE_PROXY: ProxyError,
    CurlECode.SEND_ERROR: ConnectError,
    CurlECode.RECV_ERROR: ConnectError,
    CurlECode.PARTIAL_FILE: ConnectError,
    CurlECode.GOT_NOTHING: ConnectError,
    CurlECode.HTTP2: ConnectError,
    CurlECode.HTTP2_STREAM: ConnectError,
    CurlECode.SSL_CONNECT_ERROR: TLSError,
    CurlECode.PEER_FAILED_VERIFICATION: TLSError,
    CurlECode.TOO_MANY_REDIRECTS: TooManyRedirects,
}
"""libcurl error codes and the FetchErrors they become, for errors curl-cffi raises without a specific class."""

_BODYLESS_STATUSES = frozenset({204, 304})
"""Statuses whose responses have no body, whatever `Content-Length` says."""

_DECODED_HEADERS = frozenset({"content-encoding", "content-length"})
"""Headers that describe the encoded body, and no longer apply once curl decoded it."""


class HTTPFetcher:
    """Fetches Requests over HTTP with curl-cffi, impersonating a pinned browser Profile.

    Use it as an async context manager; its curl sessions are created on first use
    and closed on exit. Every Request uses the Profile of `http.impersonate`, unless
    it names its own with `impersonate`, and Profiles never share a connection (ADR
    0010). Request headers keep their order, case, and duplicates, after those of the
    Profile; response headers keep every value.

    Each transfer is limited three ways: `http.timeout` (or `request.timeout`) to
    connect and between two pieces of data, `http.deadline` for the whole fetch,
    waiting for a connection included, and `http.max_body_size` for the body.
    Redirects are followed up to `http.max_redirects`. A body decoded from gzip,
    deflate, br, or zstd loses the `Content-Encoding` and `Content-Length` headers,
    which describe the encoded one. A 407 response from a proxy raises `ProxyError`.
    curl's own retries are off: the retry policy decides.

    Requests are independent (ADR 0011): each transfer sends only `request.cookies`,
    cookies set during it flow within its own redirect chain, and none are kept for
    later Requests. Connections are still reused.

    Attributes:
        schemes: The URL schemes it fetches by default: `http` and `https`.
        settings_sections: Registers `HTTPSettings`.
    """

    schemes: ClassVar[frozenset[str]] = frozenset({"http", "https"})
    settings_sections: ClassVar[tuple[type[Section], ...]] = (HTTPSettings,)

    def __init__(
        self,
        settings: HTTPSettings | None = None,
        *,
        max_clients: int = 16,
        default_encoding: str = "utf-8",
    ) -> None:
        """Configure the fetcher.

        Args:
            settings: The HTTP settings; the defaults when `None`.
            max_clients: The maximum number of transfers in flight at once per Profile.
            default_encoding: The `default_encoding` of the Responses.
        """
        self._settings = settings or HTTPSettings()
        self._max_clients = max_clients
        self._default_encoding = default_encoding
        self._pool: CurlSessionPool | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Build the fetcher for a Run from its settings.

        Args:
            crawler: The Crawler of the Run.

        Returns:
            A fetcher with the `http.*` settings, allowing `concurrency` transfers in
            flight, whose Responses use `response.default_encoding`.
        """
        return cls(
            crawler.settings.section(HTTPSettings),
            max_clients=crawler.settings.section(CoreSettings).concurrency,
            default_encoding=crawler.settings.section(ResponseSettings).default_encoding,
        )

    async def __aenter__(self) -> Self:
        settings = self._settings
        self._pool = CurlSessionPool(size=settings.pool_size, idle=settings.pool_idle, max_clients=self._max_clients)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            await pool.close()

    async def fetch(self, request: Request) -> Response:
        """Send a Request and read the whole response body.

        Args:
            request: The Request to send.

        Returns:
            The Response, whatever its status code.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            ValueError: `request.impersonate` is not a pinned Profile.
            pernix.errors.FetchError: The transfer failed; a subclass names the kind of
                failure, and `code` is libcurl's error code, or 0.
        """
        pool, profile = self._pool_and_profile(request)
        limit = self._settings.max_body_size or None
        started = time.monotonic()
        async with pool.session(profile) as session, self._deadline(request):
            result, transfer = await self._send(session, request, profile, limit)
            try:
                _check_declared_size(result, request, limit)
                chunks: list[bytes] = []
                size = 0
                async for chunk in _chunks(result, request, transfer):
                    size += len(chunk)
                    if limit is not None and size > limit:
                        raise ResponseTooLarge(f"the body of {request.url} exceeds {limit} bytes")
                    chunks.append(chunk)
            finally:
                await _discard(result)
        return self._response(request, result, b"".join(chunks), elapsed=time.monotonic() - started)

    @asynccontextmanager
    async def stream(self, request: Request, *, max_bytes: int | None = None) -> AsyncGenerator[StreamedResponse]:
        """Send a Request and read its body as it arrives.

        `http.deadline` limits the wait for the response headers; after that, only
        `http.timeout` between two pieces of data limits the transfer.

        Args:
            request: The Request to send.
            max_bytes: Bytes the body may have at most; `None` takes
                `http.max_body_size`, and 0 sets no limit.

        Yields:
            The Response, with its body to read once; the transfer is closed on exit.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            ValueError: `request.impersonate` is not a pinned Profile.
            pernix.errors.FetchError: The transfer failed before the headers arrived.
        """
        pool, profile = self._pool_and_profile(request)
        limit = (self._settings.max_body_size if max_bytes is None else max_bytes) or None
        started = time.monotonic()
        async with pool.session(profile) as session:
            async with self._deadline(request):
                result, transfer = await self._send(session, request, profile, limit)
            try:
                _check_declared_size(result, request, limit)
                response = self._response(request, result, b"", elapsed=time.monotonic() - started)
                yield StreamedResponse(response, lambda: _chunks(result, request, transfer), max_bytes=limit)
            finally:
                await _discard(result)

    def _pool_and_profile(self, request: Request) -> tuple[CurlSessionPool, str]:
        if self._pool is None:
            raise RuntimeError("HTTPFetcher must be entered with 'async with' before fetching")
        profile = self._settings.impersonate if request.impersonate is None else check_profile(request.impersonate)
        return self._pool, profile

    @asynccontextmanager
    async def _deadline(self, request: Request) -> AsyncGenerator[None]:
        """Limit a fetch to `http.deadline`, as a `Timeout`."""
        deadline = self._settings.deadline
        scope = asyncio.timeout(deadline)
        try:
            async with scope:
                yield
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"fetching {request.url} took longer than http.deadline ({deadline} s)") from None
            raise

    async def _send(
        self, session: CurlSession, request: Request, profile: str, limit: int | None
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Send a Request and wait for its response headers, following redirects.

        Waiting for the headers is limited precisely with asyncio; once they arrived,
        curl limits each pause in the body to whole seconds.
        """
        timeout = request.timeout or self._settings.timeout
        scope = asyncio.timeout(timeout)
        try:
            async with scope:
                result, transfer = await self._request(session, request, profile, timeout, limit)
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"{request.url} did not respond within {timeout} s") from None
            raise
        if result.status_code == 407 and request.proxy is not None:
            await _discard(result)
            raise ProxyError(f"the proxy refused {request.url}: 407 Proxy Authentication Required")
        return result, transfer

    async def _request(
        self, session: CurlSession, request: Request, profile: str, curl_timeout: float, limit: int | None
    ) -> tuple[curl_cffi.Response, Transfer]:
        try:
            return await session.start(
                cast("HttpMethod", request.method),
                request.url,
                headers=request.headers.items(),
                data=request.body,
                cookies=dict(request.cookies),
                discard_cookies=True,
                proxy=request.proxy,
                impersonate=cast("Any", profile),
                timeout=curl_timeout,
                max_redirects=self._settings.max_redirects,
                http_version=_http_version(request.url),
                body_limit=limit,
            )
        except CurlError as exc:
            raise _fetch_error(exc) from exc

    def _response(self, request: Request, result: curl_cffi.Response, body: bytes, *, elapsed: float) -> Response:
        hops = [*result.history, result]
        set_cookies = [value for hop in hops for value in hop.headers.get_list("set-cookie") if value is not None]
        headers = _headers(result)
        if request.method != "HEAD" and _decoded(headers):
            headers = Headers((name, value) for name, value in headers if name.lower() not in _DECODED_HEADERS)
        return Response(
            url=result.url,
            status=result.status_code,
            headers=headers,
            body=body,
            request=request,
            cookies=merge_set_cookies(_sent_cookies(request), set_cookies),
            default_encoding=self._default_encoding,
            history=[Redirect(hop.url, hop.status_code, _headers(hop)) for hop in result.history],
            elapsed=elapsed,
            ip_address=result.primary_ip or None,
            http_version=_HTTP_VERSIONS.get(result.http_version),
            fetcher="http",
        )


async def _chunks(result: curl_cffi.Response, request: Request, transfer: Transfer) -> AsyncIterator[bytes]:
    """Read a streamed body, as FetchErrors."""
    content = cast("AsyncIterator[bytes]", result.aiter_content())  # pyright: ignore[reportUnknownMemberType]  # untyped
    try:
        async for chunk in content:
            yield chunk
    except CurlError as exc:
        if transfer.exceeded:
            raise ResponseTooLarge(f"the body of {request.url} exceeds {transfer.limit} bytes") from exc
        raise _fetch_error(exc) from exc


async def _discard(result: curl_cffi.Response) -> None:
    """End a streamed transfer, stopping it if its body was not read to the end.

    Waiting for curl-cffi's `aclose()` would read the rest of the body first; cancelling
    the transfer instead makes curl-cffi drop it and give the handle back to its pool.
    """
    transfer = cast("asyncio.Task[None] | None", result.astream_task)  # pyright: ignore[reportUnknownMemberType]
    if transfer is None:
        return
    if not transfer.done():
        transfer.cancel()
    await asyncio.wait([transfer])


def _check_declared_size(result: curl_cffi.Response, request: Request, limit: int | None) -> None:
    """Fail at once when `Content-Length` declares a body over the limit, unless the response has no body."""
    if request.method == "HEAD" or result.status_code in _BODYLESS_STATUSES or result.status_code < 200:
        return
    declared = result.headers.get("content-length")
    if limit is not None and declared is not None and declared.isdigit() and int(declared) > limit:
        raise ResponseTooLarge(f"the body of {request.url} has {declared} bytes, more than {limit}")


def _http_version(url: str) -> CurlHttpVersion | None:
    """HTTP/1.1 for cleartext URLs, as browsers never upgrade those to HTTP/2; the Profile's choice otherwise."""
    return CurlHttpVersion.V1_1 if url[:7].lower() == "http://" else None


def _decoded(headers: Headers) -> bool:
    """Whether curl decoded the body, as it does for every encoding it does not reject."""
    return any(value.strip().lower() not in ("", "identity") for value in headers.get_all("Content-Encoding"))


def _fetch_error(error: CurlError) -> FetchError:
    """Map a curl error onto the FetchError of its kind, keeping libcurl's code."""
    code = int(error.code)
    kind = next((kind for curl_kind, kind in _ERROR_KINDS if isinstance(error, curl_kind)), None)
    return (kind or _ERROR_CODES.get(code, FetchError))(str(error), code=code)


def _headers(response: curl_cffi.Response) -> Headers:
    return Headers((name, value) for name, value in response.headers.multi_items() if value is not None)


def _sent_cookies(request: Request) -> dict[str, str]:
    """The cookies a Request carries, whether as `cookies=` or as `Cookie` headers."""
    sent: dict[str, str] = {}
    for header in request.headers.get_all("Cookie"):
        sent.update(parse_cookie_header(header))
    sent.update(request.cookies)
    return sent
