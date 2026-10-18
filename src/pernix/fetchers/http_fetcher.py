from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from curl_cffi import CurlECode, CurlError, CurlHttpVersion
from curl_cffi.requests import exceptions as curl_errors

from pernix.cookies import merge_set_cookies, request_cookies
from pernix.errors import (
    ConnectError,
    DecodeError,
    FetchError,
    NetworkBlocked,
    ProxyError,
    ResponseTooLarge,
    Timeout,
    TLSError,
    TooManyRedirects,
)
from pernix.fetchers.chain_cookies import ChainCookies
from pernix.fetchers.curl_session_pool import CurlSession, CurlSessionPool, Transfer
from pernix.fetchers.fetcher import request_proxy
from pernix.fetchers.http_settings import HTTPSettings, check_profile
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.redirects import Hop, redirect_target, redirected
from pernix.fetchers.streamed_response import StreamedResponse
from pernix.headers import Headers
from pernix.response import Redirect, Response
from pernix.settings import CoreSettings, ResponseSettings, Section
from pernix.urls import with_ascii_host

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

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
}
"""libcurl error codes and the FetchErrors they become, for errors curl-cffi raises without a specific class."""

_BODYLESS_STATUSES = frozenset({204, 304})
"""Statuses whose responses have no body, whatever `Content-Length` says."""

_DECODED_HEADERS = frozenset({"content-encoding", "content-length"})
"""Headers that describe the encoded body, and no longer apply once curl decoded it."""


@dataclass(slots=True)
class _Fetched:
    """The last response of a redirect chain, its body still to read, and what the chain gathered on the way.

    Attributes:
        result: The response.
        transfer: Its transfer.
        history: The redirects followed, oldest first.
        set_cookies: The `Set-Cookie` values of every response of the chain, in order.
    """

    result: curl_cffi.Response
    transfer: Transfer
    history: list[Redirect]
    set_cookies: list[str]


class HTTPFetcher:
    """Fetches Requests over HTTP with curl-cffi, impersonating a pinned browser Profile.

    Use it as an async context manager; its curl sessions are created on first use
    and closed on exit. Every Request uses the Profile of `http.impersonate`, unless
    it names its own with `impersonate`, and Profiles never share a connection (ADR
    0010). Request headers keep their order, case, and duplicates, after those of the
    Profile; response headers keep every value.

    Each transfer is limited three ways: `http.timeout` (or `request.timeout`) to
    connect and between two pieces of data, `http.deadline` for the whole fetch,
    waiting for a connection included, and `http.max_body_size` for the body. A
    body decoded from gzip, deflate, br, or zstd loses the `Content-Encoding` and
    `Content-Length` headers, which describe the encoded one. A proxy that cannot be
    reached, or answers 407, raises `ProxyError`.
    curl's own retries are off: the retry policy decides.

    Redirects are followed as browsers follow them (`pernix.fetchers.redirects`), up
    to `http.max_redirects`, one transfer per hop: each hop gets the cookies matching
    it, uses the HTTP version its URL calls for, and waits `http.timeout` for its
    headers. 301 and 302 turn a POST into a GET, and 303 anything but GET and HEAD;
    `Authorization` and `Proxy-Authorization` never follow a redirect to another
    origin; a `Location` that is not an http or https URL fails the fetch. The body
    of a redirect is dropped.

    Requests are independent (ADR 0011): each fetch sends only the cookies the
    Request carries, as `cookies=` or as a `Cookie` header, cookies set during it
    flow within its own redirect chain, and none are kept for later Requests.
    Connections are still reused. The Request's cookies are held for its host only,
    for every path, so they go to each hop on that host, whatever its port or scheme,
    and to no other host. Each hop gets its cookies in the order Chrome sends them,
    longer paths first, then the earliest set first, a cookie replacing another
    keeping its place: one `Cookie` header over HTTP/1.1, a `cookie` field each over
    HTTP/2.

    curl converts international hosts to ASCII itself, differently on each platform,
    so before curl sees a Request's URL, a host that is not ASCII, or holds percent
    escapes, is converted as WHATWG URLs convert hosts (`pernix.urls.with_ascii_host()`).
    Every platform then reaches the host a browser does, and `Response.url` names it
    in ASCII. A host WHATWG URLs reject fails the fetch with a `FetchError`, as
    browsers refuse such a URL. Redirects are resolved as WHATWG URLs resolve them,
    so their hosts are converted alike.

    A Request sent under a Session (`session=`) also sends the Session's cookies that
    match each hop, and the cookies set during the fetch, on redirects included, are
    kept in the Session as the headers of each hop arrive, even if a later hop fails;
    the Request's own cookies are not. The Session's proxy and
    Profile apply unless the Request names its own, an empty proxy for none, and each
    Session has curl sessions of its own, so two Sessions never share a connection.

    Under a network guard (`pernix.fetchers.network_guard`), as in tests, only
    loopback hosts are reached: each hop is checked before it is sent, and others
    raise `NetworkBlocked`.

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
            fetched = await self._follow(curl, request, session, profile, limit)
            result, transfer = fetched.result, fetched.transfer
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
        return self._response(request, session, fetched, b"".join(chunks), elapsed=time.monotonic() - started)

    @asynccontextmanager
    async def stream(
        self, request: Request, *, max_bytes: int | None = None, session: Session | None = None
    ) -> AsyncGenerator[StreamedResponse]:
        """Send a Request and read its body as it arrives.

        Only the body of the last response, after redirects, is streamed.
        `http.deadline` limits the wait for its headers; after that, only
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
                fetched = await self._follow(curl, request, session, profile, limit)
            result, transfer = fetched.result, fetched.transfer
            try:
                _check_declared_size(result, request, limit)
                elapsed = time.monotonic() - started
                response = self._response(request, session, fetched, b"", elapsed=elapsed, streamed=True)
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

    async def _follow(
        self, curl: CurlSession, request: Request, session: Session | None, profile: str, limit: int | None
    ) -> _Fetched:
        """Send a Request and wait for the headers of its last response, following redirects one hop at a time.

        The `Set-Cookie` headers of each hop apply to the cookies of the next ones, and
        the body of each redirect is dropped.
        """
        proxy = request_proxy(request, session)
        timeout = request.timeout or self._settings.timeout
        max_redirects = self._settings.max_redirects
        cookies = ChainCookies(request, session)
        hop = Hop(request.method, _curl_url(request), _without_cookies(request.headers), request.body)
        history: list[Redirect] = []
        set_cookies: list[str] = []
        redirected_from: str | None = None
        while True:
            self._check_hop(hop.url, proxy, redirected_from=redirected_from)
            result, transfer = await self._send(
                curl, hop, cookies, proxy=proxy, profile=profile, curl_timeout=timeout, limit=limit
            )
            set_cookies += _set_cookies(result)
            try:
                target = redirect_target(result.url, result.status_code, _location(result))
            except FetchError:
                await _discard(result)
                raise
            if target is None:
                return _Fetched(result, transfer, history, set_cookies)
            await _discard(result)
            history.append(Redirect(result.url, result.status_code, _headers(result)))
            if len(history) > max_redirects:
                raise TooManyRedirects(f"{request.url} redirected more than http.max_redirects ({max_redirects}) times")
            redirected_from, hop = result.url, redirected(hop, result.status_code, target)

    def _check_hop(self, url: str, proxy: str | None, *, redirected_from: str | None) -> None:
        """Refuse a hop of a redirect chain, the first included, before it is sent.

        Under a network guard, a hop to a host the guard does not admit is refused.

        Raises:
            pernix.errors.NetworkBlocked: The guard refused the hop.
        """
        guard = current_guard()
        if guard is None:
            return
        try:
            guard.check(url, proxy=proxy)
        except NetworkBlocked as exc:
            if redirected_from is None:
                raise
            raise NetworkBlocked(f"the network guard refused the redirect from {redirected_from} to {url}") from exc

    async def _send(
        self,
        curl: CurlSession,
        hop: Hop,
        cookies: ChainCookies,
        *,
        proxy: str | None,
        profile: str,
        curl_timeout: float,
        limit: int | None,
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Send one hop of a redirect chain and wait for its response headers, without following a redirect.

        Waiting for the headers is limited precisely with asyncio; once they arrived,
        curl limits each pause in the body to whole seconds.
        """
        scope = asyncio.timeout(curl_timeout)
        try:
            async with scope:
                result, transfer = await curl.start(
                    cast("HttpMethod", hop.method),
                    hop.url,
                    headers=hop.headers.items(),
                    data=hop.body,
                    cookie=cookies.header(hop.url),
                    on_headers=cookies.receive,
                    discard_cookies=True,
                    allow_redirects=False,
                    proxy=proxy,
                    impersonate=cast("Any", profile),
                    timeout=curl_timeout,
                    http_version=_http_version(hop.url),
                    body_limit=limit,
                )
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"{hop.url} did not respond within {curl_timeout} s") from None
            raise
        except CurlError as exc:
            raise _fetch_error(exc, proxied=proxy is not None) from exc
        if result.status_code == 407 and proxy is not None:
            await _discard(result)
            raise ProxyError(f"the proxy refused {hop.url}: 407 Proxy Authentication Required")
        return result, transfer

    def _response(
        self,
        request: Request,
        session: Session | None,
        fetched: _Fetched,
        body: bytes,
        *,
        elapsed: float,
        streamed: bool = False,
    ) -> Response:
        result = fetched.result
        headers = _headers(result)
        if request.method != "HEAD" and _decoded(headers):
            headers = Headers((name, value) for name, value in headers if name.lower() not in _DECODED_HEADERS)
        return Response(
            url=result.url,
            status=result.status_code,
            headers=headers,
            body=body,
            request=request,
            cookies=merge_set_cookies(request_cookies(request), fetched.set_cookies),
            default_encoding=self._default_encoding,
            history=fetched.history,
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


def _curl_url(request: Request) -> str:
    """The URL curl is given: the Request's, with its host converted as WHATWG URLs convert it, if needed."""
    try:
        return with_ascii_host(request.url)
    except ValueError as exc:
        raise FetchError(str(exc), code=CurlECode.URL_MALFORMAT) from exc


def _set_cookies(response: curl_cffi.Response) -> list[str]:
    return [value for value in response.headers.get_list("set-cookie") if value is not None]


def _location(response: curl_cffi.Response) -> str | None:
    """The first non-empty `Location` of a response, with its bytes that are not ASCII percent-escaped, as Chrome reads it."""
    for name, value in response.headers.raw:
        if name.strip().lower() == b"location" and value is not None and (location := value.strip()):
            return "".join(chr(byte) if byte < 0x80 else f"%{byte:02X}" for byte in location)
    return None


def _without_cookies(headers: Headers) -> Headers:
    """Request headers without `Cookie`, whose cookies each hop gets with the others."""
    return Headers((name, value) for name, value in headers if name.lower() != "cookie")


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
