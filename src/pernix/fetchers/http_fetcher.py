from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Self, cast

from curl_cffi import CurlECode, CurlError, CurlHttpVersion
from curl_cffi.requests import exceptions as curl_errors
from curl_cffi.requests.utils import requote_uri

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
from pernix.fetchers.address_guard import AddressGuard
from pernix.fetchers.chain_cookies import ChainCookies
from pernix.fetchers.chain_hints import ChainHints
from pernix.fetchers.chrome_headers import ChromeRequest, HopHints, chrome_request, through_proxy, tunnel_headers
from pernix.fetchers.client_hints import DEFAULT_HINTS, HintValues, sent_hints
from pernix.fetchers.curl_session_pool import ClientHintsCallback, CurlSession, CurlSessionPool, Transfer
from pernix.fetchers.fetcher import request_proxy
from pernix.fetchers.http_settings import DEFAULT_PROFILE, HTTPSettings, check_profile
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.pinned_pair import client_headers
from pernix.fetchers.redirects import Hop, redirect_check, redirect_limit, redirect_target, redirected
from pernix.fetchers.streamed_response import StreamedResponse
from pernix.headers import Headers
from pernix.request_kinds import is_document, request_kind
from pernix.response import Redirect, Response
from pernix.settings import DEFAULT_LOCALE, CoreSettings, NetworkSettings, ResponseSettings, Section, locale_languages
from pernix.urls import url_host, url_origin, with_ascii_host

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Sequence

    import curl_cffi
    from curl_cffi.requests.session import HttpMethod

    from pernix.crawler import Crawler
    from pernix.fetchers.address_guard import PinnedHost
    from pernix.request import Request
    from pernix.request_kinds import RequestKind
    from pernix.session import Session

__all__ = ["HTTPFetcher"]

logger = logging.getLogger(__name__)


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
    to `http.max_redirects`, or fewer inside `following_at_most()`, one transfer per
    hop: each hop gets the cookies matching it, uses the HTTP version its URL calls
    for, and waits `http.timeout` for its headers. 301 and 302 turn a POST into a
    GET, and 303 anything but GET and HEAD;
    `Authorization` and `Proxy-Authorization` never follow a redirect to another
    origin; a `Location` that is not an http or https URL fails the fetch. The body
    of a redirect is dropped. Inside `checking_redirects()`, as with `robots.obey`,
    each redirect is checked before its hop is sent, the time the check takes
    added to `http.deadline`.

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

    With the Profile of the Pinned Pair (ADR 0010), each hop carries the headers
    Chrome sends for the kind of request the Request is (`pernix.request_kinds`),
    in Chrome's order, with its HTTP/2 stream weight
    (`pernix.fetchers.chrome_headers`): those that name the client are the bundled
    browser's on the platform the Run is on, for the languages of `locale`
    (`pernix.fetchers.pinned_pair`), and the `Referer`, `Origin`, and
    `Sec-Fetch-Site` are made from the page the Request's `Referer` names. A
    header of the Request's own replaces the kind's of its name in place; another
    goes after them in a navigation, and first among those a page adds in the
    other kinds. A `fetch()` to another origin than its page's sends only the
    Request's own cookies, and keeps none it is sent. Another Profile, from the
    settings, the Request, or the Session, sends the Request's headers as they are,
    after its own.

    The responses of a top-level navigation or form submission, its redirects
    included, ask for client hints with `Accept-CH`, for their origin
    (`pernix.fetchers.client_hints`). With the pinned Profile, the hints an origin
    asked for go with the navigations to it, and with the requests its pages make to
    it, as Chrome sends them, in its order; those a navigation sends stay on when it
    is redirected, even to another origin, as Chrome's do. A navigation (a GET) whose
    response's `Critical-CH` names a hint its `Accept-CH` asks for that it did not
    send is sent again from the Request's URL, once for each origin, with the hints:
    the response dropped is not a redirect, and is not in `Response.history`, but
    the cookies it set are kept.

    A Request sent under a Session (`session=`) also sends the Session's cookies that
    match each hop, and the cookies set during the fetch, on redirects included, are
    kept in the Session as the headers of each hop arrive, even if a later hop fails;
    the Request's own cookies are not. So are the client hints its responses ask
    for, which a fetch without a Session keeps within its own redirect chain only.
    The Session's proxy and Profile apply unless the Request names its own, an empty
    proxy for none, and each Session has curl sessions of its own, so two Sessions
    never share a connection.

    Under a network guard (`pernix.fetchers.network_guard`), as in tests, only
    loopback hosts are reached: each hop is checked before it is sent, and others
    raise `NetworkBlocked`.

    With `network.public_only`, only hosts on public addresses are reached
    (`pernix.fetchers.address_guard`): each hop is checked before it is sent, its
    host resolved unless written as an address, and curl connects only to the
    addresses checked; others raise `NetworkBlocked`. Through a proxy, which
    resolves hosts itself, only hosts written as addresses, and loopback names,
    are checked; curl resolves them itself for a `socks4://` or `socks5://` one,
    so through one they are resolved, checked, and pinned too. The hosts of
    `network.allow_hosts` are not checked.

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
        network: NetworkSettings | None = None,
        locale: str | Sequence[str] = DEFAULT_LOCALE,
    ) -> None:
        """Configure the fetcher.

        Args:
            settings: The HTTP settings; the defaults when `None`.
            max_clients: The maximum number of transfers in flight at once per Profile.
            default_encoding: The `default_encoding` of the Responses.
            network: Which hosts may be reached; any when `None`.
            locale: The languages presented with the pinned Profile, as the `locale`
                setting names them.

        Raises:
            ValueError: `locale` names no language, or something else.
        """
        self._settings = settings or HTTPSettings()
        self._client_headers = client_headers(locale_languages(locale))
        self._hint_values = HintValues()
        self._max_clients = max_clients
        self._default_encoding = default_encoding
        self._address_guard = (
            AddressGuard(frozenset(network.allow_hosts)) if network is not None and network.public_only else None
        )
        self._pool: CurlSessionPool | None = None

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Build the fetcher for a Run from its settings.

        Args:
            crawler: The Crawler of the Run.

        Returns:
            A fetcher with the `http.*` settings, allowing `concurrency` transfers in
            flight, whose Responses use `response.default_encoding`, reaching the
            hosts the `network.*` settings allow, and presenting the languages of
            `locale`. A Profile other than the pinned one is warned about.
        """
        settings = crawler.settings.section(HTTPSettings)
        core = crawler.settings.section(CoreSettings)
        if settings.impersonate != DEFAULT_PROFILE:
            logger.warning(
                "%s is %r, not %r, the Profile of the Pinned Pair: the HTTP Fetcher presents another client than "
                "the browser, with that Profile's own User-Agent and client hints",
                HTTPSettings.key("impersonate"),
                settings.impersonate,
                DEFAULT_PROFILE,
            )
        return cls(
            settings,
            max_clients=core.concurrency,
            default_encoding=crawler.settings.section(ResponseSettings).default_encoding,
            network=crawler.settings.section(NetworkSettings),
            locale=core.locale,
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
            ValueError: The Profile to use is not a pinned one, `request.session`
                names another Session, or, with the pinned Profile, its
                `Sec-Fetch-Dest` names no kind of request pernix makes.
            pernix.errors.FetchError: The transfer failed, or WHATWG URLs reject the
                host; a subclass names the kind of failure, and `code` is libcurl's
                error code, or 0.
        """
        pool, profile = self._pool_and_profile(request, session)
        limit = self._settings.max_body_size or None
        started = time.monotonic()
        async with pool.session(profile, owner=_owner(session)) as curl, self._deadline(request) as deadline:
            fetched = await self._follow(curl, request, session, profile, limit, deadline)
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
            async with self._deadline(request) as deadline:
                fetched = await self._follow(curl, request, session, profile, limit, deadline)
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
    async def _deadline(self, request: Request) -> AsyncGenerator[asyncio.Timeout]:
        """Limit a fetch to `http.deadline`, as a `Timeout`; the scope it gives may be moved on."""
        deadline = self._settings.deadline
        scope = asyncio.timeout(deadline)
        try:
            async with scope:
                yield scope
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"fetching {request.url} took longer than http.deadline ({deadline} s)") from None
            raise

    async def _follow(
        self,
        curl: CurlSession,
        request: Request,
        session: Session | None,
        profile: str,
        limit: int | None,
        deadline: asyncio.Timeout,
    ) -> _Fetched:
        """Send a Request and wait for the headers of its last response, following redirects one hop at a time.

        The `Set-Cookie` headers of each hop apply to the cookies of the next ones, as
        the `Accept-CH` of a top-level navigation's apply to its client hints, and the
        body of each redirect is dropped. A navigation whose `Critical-CH` needs hints
        it did not send starts again. `deadline`, the scope of `http.deadline`, is
        moved on by the time the checks of the redirects take.
        """
        proxy = request_proxy(request, session)
        timeout = request.timeout or self._settings.timeout
        limited = redirect_limit(self._settings.max_redirects)
        max_redirects = self._settings.max_redirects if limited is None else limited
        cookies = ChainCookies(request, session)
        hints = ChainHints(session)
        top_level = _top_level(request)
        first = Hop(request.method, _curl_url(request), _without_cookies(request.headers), request.body)
        hop = first
        chrome = (
            _ChromeHops(request, self._client_headers, hints, self._hint_values) if profile == DEFAULT_PROFILE else None
        )
        history: list[Redirect] = []
        set_cookies: list[str] = []
        restarted: set[str | None] = set()
        redirected_from: str | None = None
        missing: list[tuple[str, ...]] = []  # the hints a response's Critical-CH needs, as its headers arrive

        def receive_hints(url: str, accept_ch: str | None, critical_ch: str | None) -> None:
            missing.append(hints.receive(url, accept_ch, critical_ch))

        while True:
            pinned = await self._check_hop(
                hop.url, proxy, resolve_timeout=timeout, redirected_from=redirected_from, deadline=deadline
            )
            sent = None if chrome is None else chrome.next(hop)
            missing.clear()
            result, transfer = await self._send(
                curl,
                hop,
                cookies,
                proxy=proxy,
                profile=profile,
                curl_timeout=timeout,
                limit=limit,
                pinned=pinned,
                sent=sent,
                on_client_hints=receive_hints if top_level else None,
            )
            if sent is None or sent.credentials:  # a response no cookie goes with sets none
                set_cookies += _set_cookies(result)
            needed = missing[-1] if missing else ()
            if chrome is not None and url_origin(hop.url) not in restarted and chrome.restarts(hop.url, needed):
                logger.debug("Sending %s again with the client hints the Critical-CH of %s needs", request.url, hop.url)
                await _discard(result)
                restarted.add(url_origin(hop.url))
                chrome.restart()
                history, redirected_from, hop = [], None, first
                continue
            try:
                target = redirect_target(result.url, result.status_code, _location(result))
            except FetchError:
                await _discard(result)
                raise
            if target is None:
                transfer.limit = limit  # lifted for a response that may have been dropped for Critical-CH
                return _Fetched(result, transfer, history, set_cookies)
            await _discard(result)
            history.append(Redirect(result.url, result.status_code, _headers(result)))
            if len(history) > max_redirects:
                most = f"http.max_redirects ({max_redirects})" if limited is None else str(max_redirects)
                raise TooManyRedirects(f"{request.url} redirected more than {most} times")
            redirected_from, hop = result.url, redirected(hop, result.status_code, requote_uri(target))

    async def _check_hop(
        self,
        url: str,
        proxy: str | None,
        *,
        resolve_timeout: float,
        redirected_from: str | None,
        deadline: asyncio.Timeout,
    ) -> PinnedHost | None:
        """Refuse a hop of a redirect chain, the first included, before it is sent.

        Under a network guard, a hop to a host the guard does not admit is refused;
        with `network.public_only`, a hop to a host not on public addresses, which
        is resolved within `resolve_timeout`. A redirect the guards let through is
        then checked with the check in effect, if any
        (`pernix.fetchers.redirects.redirect_check()`), `deadline` moved on by the
        time it takes.

        Returns:
            The host and the addresses the hop may connect to, once resolved.

        Raises:
            pernix.errors.NetworkBlocked: A guard refused the hop.
            pernix.errors.ConnectError: The host cannot be resolved.
            pernix.errors.Timeout: Resolving the host took longer than `resolve_timeout`.
            Exception: The check refused the redirect, such as with `IgnoreRequest`.
        """
        guard = current_guard()
        if guard is not None:
            try:
                guard.check(url, proxy=proxy)
            except NetworkBlocked as exc:
                if redirected_from is None:
                    raise
                raise NetworkBlocked(f"the network guard refused the redirect from {redirected_from} to {url}") from exc
        pinned = await self._resolved(url, proxy, resolve_timeout, redirected_from)
        if redirected_from is not None:
            await _checked_redirect(url, deadline)
        return pinned

    async def _resolved(
        self, url: str, proxy: str | None, resolve_timeout: float, redirected_from: str | None
    ) -> PinnedHost | None:
        """Check the host of a hop with the address guard, if on, within `resolve_timeout`."""
        if self._address_guard is None:
            return None
        scope = asyncio.timeout(resolve_timeout)
        try:
            async with scope:
                return await self._address_guard.check(url, proxy=proxy, redirected_from=redirected_from)
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"resolving the host of {url} took longer than {resolve_timeout} s") from None
            raise

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
        pinned: PinnedHost | None,
        sent: ChromeRequest | None,
        on_client_hints: ClientHintsCallback | None = None,
    ) -> tuple[curl_cffi.Response, Transfer]:
        """Send one hop of a redirect chain and wait for its response headers, without following a redirect.

        Waiting for the headers is limited precisely with asyncio; once they arrived,
        curl limits each pause in the body to whole seconds. A pinned host is
        reached at the addresses it was checked at. With the pinned Profile, `sent`
        holds the headers Chrome sends, which replace curl's own, as Chrome sends
        them through an HTTP proxy, if any, with the `CONNECT` it sends for a tunnel.
        `on_client_hints` is told of the client hints the response asks for as its
        headers arrive, as the cookies it sets are kept then, even if its body fails.
        """
        credentials = sent is None or sent.credentials
        shaped: dict[str, Any] = {}
        if sent is not None:
            tunnel: list[tuple[str, str]] = []
            if _http_proxy(proxy):
                if hop.url[:7].lower() == "http://":
                    sent = through_proxy(sent)
                else:
                    tunnel = tunnel_headers(sent)
            shaped = {
                "default_headers": False,
                "header_order": sent.order,
                "stream_weight": sent.weight,
                "proxy_headers": [f"{name}: {value}" for name, value in tunnel],
            }
        scope = asyncio.timeout(curl_timeout)
        try:
            async with scope:
                result, transfer = await curl.start(
                    cast("HttpMethod", hop.method),
                    hop.url,
                    headers=hop.headers.items() if sent is None else sent.headers,
                    data=hop.body,
                    cookie=cookies.header(hop.url, credentials=credentials),
                    on_headers=cookies.receive if credentials else None,
                    discard_cookies=True,
                    allow_redirects=False,
                    quote=False,  # the URL is quoted already, so cookies match the one sent
                    proxy=proxy,
                    impersonate=cast("Any", profile),
                    timeout=curl_timeout,
                    http_version=_http_version(hop.url),
                    body_limit=limit,
                    resolve=_resolve_entries(pinned),
                    on_client_hints=on_client_hints,
                    **shaped,
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


class _ChromeHops:
    """Makes the headers Chrome sends on each hop of a Request's redirect chain, with the pinned Profile.

    A navigation, a form submission, or an iframe's sends the client hints of the
    origin of its first hop, and adds those of each origin it is redirected to that
    it did not send yet. A page's other requests send the page's hints when they go
    to the page's origin first, and keep them.
    """

    def __init__(self, request: Request, client: list[tuple[str, str]], hints: ChainHints, values: HintValues) -> None:
        """Read the kind of a Request, and the page it comes from.

        Raises:
            ValueError: Its `Sec-Fetch-Dest` names no kind pernix makes.
        """
        self._kind: RequestKind = request_kind(request)
        self._referrer = request.headers.get("Referer") or None
        self._client = client
        self._hints = hints
        self._values = values
        self._chain: list[str] = []
        self._first: tuple[str, ...] = ()
        self._added: list[str] = []
        self._restarted = False
        self._first_sent: frozenset[str] = frozenset()
        """The names of the headers the first hop sent, as Chrome compares the hints `Critical-CH` needs to."""

    def next(self, hop: Hop) -> ChromeRequest:
        """The headers of the next hop, the first included."""
        self._chain.append(hop.url)
        sent = chrome_request(
            self._kind,
            chain=self._chain,
            method=hop.method,
            own=hop.headers.items(),
            referrer=self._referrer,
            client=self._client,
            hints=self._placed(hop.url),
        )
        if len(self._chain) == 1:
            self._first_sent = frozenset(sent.order.split(","))
        return sent

    def restarts(self, url: str, missing: tuple[str, ...]) -> bool:
        """Whether a navigation starts again for the hints a response's `Critical-CH` needs, as Chrome does.

        Args:
            url: The URL of the response.
            missing: The hints its `Critical-CH` needs that its origin had not asked
                for, from `ChainHints.receive()`.

        Returns:
            Whether the Request is a navigation, a GET, and its first hop did not
            send a hint its origin now asks for.
        """
        if self._kind != "navigation" or not missing:
            return False
        return any(hint not in self._first_sent for hint in sent_hints(self._hints.asked(url)))

    def restart(self) -> None:
        """Start the chain again, with every client hint after `Accept`, as Chrome sends a navigation again."""
        self._chain = []
        self._added = []
        self._restarted = True

    def _placed(self, url: str) -> HopHints:
        """The client hints of the hop to a URL, the last of the chain."""
        wanted = self._wanted(url)
        if len(self._chain) == 1:
            self._first = wanted
        elif is_document(self._kind):
            carried = {*DEFAULT_HINTS, *self._first, *self._added}
            self._added += [hint for hint in sent_hints(wanted) if hint not in carried]
        return HopHints(self._first, tuple(self._added), self._restarted, self._values.values(url_host(url) or ""))

    def _wanted(self, url: str) -> tuple[str, ...]:
        """The hints asked for by the origin that a hop to a URL sends those of, as Chrome picks it."""
        if self._kind in ("navigation", "form"):
            return self._hints.asked(url)
        page = (url_origin(self._referrer) if self._referrer else None) or url_origin(self._chain[0])
        return self._hints.asked(url) if url_origin(url) == page else ()


async def _checked_redirect(url: str, deadline: asyncio.Timeout) -> None:
    """Check a redirect with the check in effect, if any, moving the fetch's deadline on by the time it takes.

    The check may fetch, as robots.txt is fetched, under deadlines of its own, so the
    fetch's deadline is lifted meanwhile.
    """
    check = redirect_check()
    if check is None:
        return
    when = deadline.when()
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline.reschedule(None)
    try:
        await check(url)
    finally:
        if when is not None:
            deadline.reschedule(when + loop.time() - started)


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
    """The URL curl is given: the Request's, with its host converted as WHATWG URLs convert it, if needed.

    It is quoted as curl-cffi quotes URLs, so that `/café` is `/caf%C3%A9`, the path
    the cookies of the first hop match.
    """
    try:
        return requote_uri(with_ascii_host(request.url))
    except ValueError as exc:
        raise FetchError(str(exc), code=CurlECode.URL_MALFORMAT) from exc


def _resolve_entries(pinned: PinnedHost | None) -> list[str]:
    """The `CURLOPT_RESOLVE` entry pinning a host to the addresses checked, expiring as curl's cached ones do."""
    if pinned is None:
        return []
    addresses = ",".join(f"[{address}]" if ":" in address else address for address in pinned.addresses)
    return [f"+{pinned.host}:{pinned.port}:{addresses}"]


def _top_level(request: Request) -> bool:
    """Whether a Request is a top-level navigation or a form submission, whose responses ask for client hints."""
    try:
        return request_kind(request) in ("navigation", "form")
    except ValueError:
        return False  # a destination pernix makes no request for, sent as it is by another Profile


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


def _http_proxy(proxy: str | None) -> bool:
    """Whether a proxy is an HTTP one, `http` or `https`, which a request reaches its server through with `CONNECT`, or as a whole."""
    if not proxy:
        return False
    scheme, separator, _rest = proxy.partition("://")
    return not separator or scheme.lower() in ("http", "https")


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
