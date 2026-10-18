from __future__ import annotations

import asyncio
import re
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from curl_cffi import CurlECode, CurlError, CurlHttpVersion
from curl_cffi.requests import exceptions as curl_errors

from pernix.cookies import merge_set_cookies, request_cookies, requires_secure
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
from pernix.fetchers.fetcher import request_proxy
from pernix.fetchers.http_settings import HTTPSettings, check_profile
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.streamed_response import StreamedResponse
from pernix.headers import Headers
from pernix.response import Redirect, Response
from pernix.settings import CoreSettings, ResponseSettings, Section
from pernix.urls import url_host, with_ascii_host

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Mapping

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

    from pernix.cookie_jar import Cookie
    from pernix.crawler import Crawler
    from pernix.request import Request
    from pernix.session import Session

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

_NOT_IN_LINE = re.compile(r"[\x00-\x1f\x7f]")
"""Characters a line of curl's cookie file format cannot carry in its fields: the tab separates them."""

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
    which describe the encoded one. A proxy that cannot be reached, or answers 407,
    raises `ProxyError`.
    curl's own retries are off: the retry policy decides.

    Requests are independent (ADR 0011): each transfer sends only `request.cookies`,
    cookies set during it flow within its own redirect chain, and none are kept for
    later Requests. Connections are still reused. `request.cookies` are held for the
    Request's host only, for every path, so they go to each hop on that host,
    whatever its port or scheme, and to no other host. curl sends longer cookie names
    first, and names of one length in the order given: one `Cookie` header over
    HTTP/1.1, a `cookie` field each over HTTP/2.

    curl converts international hosts to ASCII itself, differently on each platform,
    so before curl sees a Request's URL, a host that is not ASCII, or holds percent
    escapes, is converted as WHATWG URLs convert hosts (`pernix.urls.with_ascii_host()`).
    Every platform then reaches the host a browser does, and `Response.url` names it
    in ASCII. A host WHATWG URLs reject fails the fetch with a `FetchError`, as
    browsers refuse such a URL. The hosts of redirects are still converted by curl.

    A Request sent under a Session (`session=`) also sends the Session's cookies that
    match each hop, and the cookies set during the fetch, on redirects included, are
    kept in the Session as the headers of each hop arrive, even if a later hop fails;
    the Request's own cookies are not. The Session's proxy and
    Profile apply unless the Request names its own, an empty proxy for none, and each
    Session has curl sessions of its own, so two Sessions never share a connection.

    Under a network guard (`pernix.fetchers.network_guard`), as in tests, only
    loopback hosts are reached, redirects included; others raise `NetworkBlocked`.

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

    async def fetch(self, request: Request, *, session: Session | None = None) -> Response:
        """Send a Request and read the whole response body.

        Args:
            request: The Request to send.
            session: The Session to send it under, if any.

        Returns:
            The Response, whatever its status code.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            ValueError: The Profile to use is not a pinned one, or `request.session`
                names another Session.
            pernix.errors.FetchError: The transfer failed, or WHATWG URLs reject the
                host; a subclass names the kind of failure, and `code` is libcurl's
                error code, or 0.
        """
        pool, profile = self._pool_and_profile(request, session)
        limit = self._settings.max_body_size or None
        started = time.monotonic()
        async with pool.session(profile, owner=_owner(session)) as curl, self._deadline(request):
            result, transfer = await self._send(curl, request, session, profile, limit)
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
        return self._response(request, session, result, b"".join(chunks), elapsed=time.monotonic() - started)

    @asynccontextmanager
    async def stream(
        self, request: Request, *, max_bytes: int | None = None, session: Session | None = None
    ) -> AsyncGenerator[StreamedResponse]:
        """Send a Request and read its body as it arrives.

        `http.deadline` limits the wait for the response headers; after that, only
        `http.timeout` between two pieces of data limits the transfer. A slow reader
        holds about a megabyte of the body in memory at most: curl stops receiving
        until it catches up, and pausing never times out.

        Args:
            request: The Request to send.
            max_bytes: Bytes the body may have at most; `None` takes
                `http.max_body_size`, and 0 sets no limit.
            session: The Session to send it under, if any.

        Yields:
            The Response, with its body to read once; the transfer is closed on exit.
            The Response is marked `streamed`.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            ValueError: The Profile to use is not a pinned one, or `request.session`
                names another Session.
            pernix.errors.FetchError: The transfer failed before the headers arrived, or
                WHATWG URLs reject the host.
        """
        pool, profile = self._pool_and_profile(request, session)
        limit = (self._settings.max_body_size if max_bytes is None else max_bytes) or None
        started = time.monotonic()
        async with pool.session(profile, owner=_owner(session)) as curl:
            async with self._deadline(request):
                result, transfer = await self._send(curl, request, session, profile, limit)
            try:
                _check_declared_size(result, request, limit)
                elapsed = time.monotonic() - started
                response = self._response(request, session, result, b"", elapsed=elapsed, streamed=True)
                yield StreamedResponse(response, lambda: _chunks(result, request, transfer), max_bytes=limit)
            finally:
                await _discard(result)

    def _pool_and_profile(self, request: Request, session: Session | None) -> tuple[CurlSessionPool, str]:
        if self._pool is None:
            raise RuntimeError("HTTPFetcher must be entered with 'async with' before fetching")
        if session is not None and request.session is not None and request.session != session.id:
            raise ValueError(f"the Request names the Session {request.session!r}, but is sent under {session.id!r}")
        named = request.impersonate or (session.profile if session is not None else None)
        return self._pool, self._settings.impersonate if named is None else check_profile(named)

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
        self, curl: CurlSession, request: Request, session: Session | None, profile: str, limit: int | None
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Send a Request and wait for its response headers, following redirects.

        Waiting for the headers is limited precisely with asyncio; once they arrived,
        curl limits each pause in the body to whole seconds.
        """
        timeout = request.timeout or self._settings.timeout
        proxy = request_proxy(request, session)
        scope = asyncio.timeout(timeout)
        try:
            async with scope:
                result, transfer = await self._request(curl, request, session, proxy, profile, timeout, limit)
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"{request.url} did not respond within {timeout} s") from None
            raise
        if result.status_code == 407 and proxy is not None:
            await _discard(result)
            raise ProxyError(f"the proxy refused {request.url}: 407 Proxy Authentication Required")
        return result, transfer

    async def _request(
        self,
        curl: CurlSession,
        request: Request,
        session: Session | None,
        proxy: str | None,
        profile: str,
        curl_timeout: float,
        limit: int | None,
    ) -> tuple[curl_cffi.Response, Transfer]:
        url = _curl_url(request)
        guard = current_guard()
        if guard is not None:
            guard.check(url, proxy=proxy)
        try:
            return await curl.start(
                cast("HttpMethod", request.method),
                url,
                headers=request.headers.items(),
                data=request.body,
                cookie_lines=_cookie_lines(request, session),
                on_headers=None if session is None else session.cookies.receive,
                discard_cookies=True,
                proxy=proxy,
                impersonate=cast("Any", profile),
                timeout=curl_timeout,
                max_redirects=self._settings.max_redirects,
                http_version=_http_version(url),
                body_limit=limit,
                guard=None if proxy else guard,  # a loopback proxy takes every hop
            )
        except CurlError as exc:
            raise _fetch_error(exc, proxied=proxy is not None) from exc

    def _response(
        self,
        request: Request,
        session: Session | None,
        result: curl_cffi.Response,
        body: bytes,
        *,
        elapsed: float,
        streamed: bool = False,
    ) -> Response:
        set_cookies = [value for hop in [*result.history, result] for value in _set_cookies(hop)]
        headers = _headers(result)
        if request.method != "HEAD" and _decoded(headers):
            headers = Headers((name, value) for name, value in headers if name.lower() not in _DECODED_HEADERS)
        return Response(
            url=result.url,
            status=result.status_code,
            headers=headers,
            body=body,
            request=request,
            cookies=merge_set_cookies(request_cookies(request), set_cookies),
            default_encoding=self._default_encoding,
            history=[Redirect(hop.url, hop.status_code, _headers(hop)) for hop in result.history],
            elapsed=elapsed,
            ip_address=result.primary_ip or None,
            http_version=_HTTP_VERSIONS.get(result.http_version),
            session=None if session is None else session.id,
            fetcher="http",
            streamed=streamed,
        )


async def _chunks(result: curl_cffi.Response, request: Request, transfer: Transfer) -> AsyncIterator[bytes]:
    """Read a streamed body, resuming curl as it is read, and failing with FetchErrors."""
    content = cast("AsyncIterator[bytes]", result.aiter_content())  # pyright: ignore[reportUnknownMemberType]  # untyped
    try:
        async for chunk in content:
            transfer.read(len(chunk))
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


def _cookie_lines(request: Request, session: Session | None) -> list[str]:
    """The cookies to send, as lines of curl's cookie file format: the Session's, then `request.cookies`.

    curl matches them to each hop itself. `request.cookies` are held for the
    Request's host only, for every path, replacing a Session cookie held so; curl-cffi
    would instead hold them for a host of its own, with `.local` appended to names
    without a dot, which curl then never sends them to. Names with a prefix browsers
    take only on Secure cookies are Secure, as curl requires too. A URL that names no
    host, such as `http:///path`, which curl reads otherwise, holds none. Session
    cookies with control characters, which the format cannot carry, are left out.
    Among cookies whose paths, domains, and names are as long, curl sends the newest
    first, so the lines go in reverse.
    """
    host = url_host(request.url)
    own: Mapping[str, str] = request.cookies if host is not None else {}
    kept = [] if session is None else [cookie for cookie in session.cookies if not _replaced(cookie, host, own)]
    lines = [_cookie_line(cookie) for cookie in kept if not _NOT_IN_LINE.search(_line_fields(cookie))]
    lines += [
        "\t".join((host or "", "FALSE", "/", "TRUE" if requires_secure(name) else "FALSE", "0", name, value))
        for name, value in own.items()
    ]
    return lines[::-1]


def _curl_url(request: Request) -> str:
    """The URL curl is given: the Request's, with its host converted as WHATWG URLs convert it, if needed."""
    try:
        return with_ascii_host(request.url)
    except ValueError as exc:
        raise FetchError(str(exc), code=CurlECode.URL_MALFORMAT) from exc


def _set_cookies(response: curl_cffi.Response) -> list[str]:
    return [value for value in response.headers.get_list("set-cookie") if value is not None]


def _cookie_line(cookie: Cookie) -> str:
    expires = "0" if cookie.expires is None else str(max(1, int(cookie.expires)))  # 0 is a cookie without expiry
    return "\t".join(
        (
            cookie.domain,
            "FALSE" if cookie.host_only else "TRUE",
            cookie.path,
            "TRUE" if cookie.secure else "FALSE",
            expires,
            cookie.name,
            cookie.value,
        )
    )


def _line_fields(cookie: Cookie) -> str:
    return cookie.domain + cookie.path + cookie.name + cookie.value


def _replaced(cookie: Cookie, host: str | None, own: Mapping[str, str]) -> bool:
    """Whether a Session cookie is the one a cookie of the Request, held for its host and the path /, replaces."""
    return cookie.host_only and cookie.domain == host and cookie.path == "/" and cookie.name in own


def _owner(session: Session | None) -> str | None:
    return None if session is None else session.id


def _http_version(url: str) -> CurlHttpVersion | None:
    """HTTP/1.1 for cleartext URLs, as browsers never upgrade those to HTTP/2; the Profile's choice otherwise."""
    return CurlHttpVersion.V1_1 if url[:7].lower() == "http://" else None


def _decoded(headers: Headers) -> bool:
    """Whether curl decoded the body, as it does for every encoding it does not reject."""
    return any(value.strip().lower() not in ("", "identity") for value in headers.get_all("Content-Encoding"))


def _fetch_error(error: CurlError, *, proxied: bool = False) -> FetchError:
    """Map a curl error onto the FetchError of its kind, keeping libcurl's code.

    Through a proxy, curl connects to the proxy alone, so failing to connect is the
    proxy's failure.
    """
    code = int(error.code)
    if proxied and code == CurlECode.COULDNT_CONNECT:
        return ProxyError(str(error), code=code)
    kind = next((kind for curl_kind, kind in _ERROR_KINDS if isinstance(error, curl_kind)), None)
    return (kind or _ERROR_CODES.get(code, FetchError))(str(error), code=code)


def _headers(response: curl_cffi.Response) -> Headers:
    return Headers((name, value) for name, value in response.headers.multi_items() if value is not None)
