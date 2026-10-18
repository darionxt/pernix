from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar, Self
from urllib.parse import urldefrag, urlsplit

from pernix.browser import Render, RenderResult
from pernix.cookies import request_cookies
from pernix.errors import (
    ActionError,
    ComponentError,
    ConnectError,
    FetchError,
    NetworkBlocked,
    PernixError,
    ProxyError,
    Timeout,
    TLSError,
    TooManyRedirects,
)
from pernix.fetchers.browser_cookies import request_cookie
from pernix.fetchers.browser_settings import BrowserSettings
from pernix.fetchers.fetcher import request_proxy
from pernix.fetchers.launched_browser import LaunchedBrowser, launch_browser
from pernix.fetchers.network_guard import admitted, current_guard
from pernix.fetchers.page_actions import run_render
from pernix.fetchers.page_slots import PageSlot, PageSlots
from pernix.fetchers.page_traffic import ResourceBlocker, ResponseCapture, body_headers, captured_response
from pernix.fetchers.session_contexts import SessionContexts
from pernix.headers import Headers
from pernix.response import Redirect, Response
from pernix.settings import ResponseSettings, Section
from pernix.urls import url_host

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Iterable

    from patchright._impl._api_structures import NameValue, SetCookieParam
    from patchright.async_api import BrowserContext, Frame, Page, Route, WebSocket
    from patchright.async_api import Request as PageRequest
    from patchright.async_api import Response as PageResponse

    from pernix.crawler import Crawler
    from pernix.fetchers.network_guard import NetworkGuard
    from pernix.fetchers.session_contexts import SessionContext
    from pernix.request import Request
    from pernix.session import Session
    from pernix.stats import Stats

__all__ = ["BrowserFetcher"]

logger = logging.getLogger(__name__)

_RENDERED_TYPES = frozenset({"text/html", "application/xhtml+xml"})
"""Documents whose rendered DOM is the body; Chrome shows others, such as JSON or images, in a viewer page of its own."""

_NO_CONTENT = frozenset({204, 205})
"""Statuses that make Chrome abandon the navigation, keeping the page it was on."""

_NETWORK_SCHEMES = frozenset({"http", "https", "ws", "wss"})

_CREDENTIAL_HEADERS = frozenset({"authorization", "proxy-authorization"})
"""Headers the browser would send on to every host a redirect leads to."""

_NET_ERROR = re.compile(r"net::(ERR_[A-Z0-9_]+)")

_NET_ERROR_KINDS: tuple[tuple[str, type[FetchError]], ...] = (
    ("ERR_PROXY_", ProxyError),
    ("ERR_TUNNEL_CONNECTION_FAILED", ProxyError),
    ("ERR_MANDATORY_PROXY_", ProxyError),
    ("ERR_CERT_", TLSError),
    ("ERR_SSL_", TLSError),
    ("ERR_TOO_MANY_REDIRECTS", TooManyRedirects),
    ("ERR_TIMED_OUT", Timeout),
    ("ERR_CONNECTION_TIMED_OUT", Timeout),  # before ERR_CONNECTION_
    ("ERR_NAME_", ConnectError),
    ("ERR_ADDRESS_", ConnectError),
    ("ERR_CONNECTION_", ConnectError),
    ("ERR_EMPTY_RESPONSE", ConnectError),
    ("ERR_INTERNET_DISCONNECTED", ConnectError),
    ("ERR_NETWORK_", ConnectError),
    ("ERR_SOCKET_", ConnectError),
    ("ERR_HTTP2_", ConnectError),
    ("ERR_QUIC_", ConnectError),
    ("ERR_INCOMPLETE_CHUNKED_ENCODING", ConnectError),
    ("ERR_CONTENT_LENGTH_MISMATCH", ConnectError),
)
"""Chrome's network errors, by prefix, and the FetchErrors they become; the first match wins."""


class BrowserFetcher:
    """Fetches Requests by loading them in a browser: Chrome for Testing, driven by patchright.

    Registered as `browser`, it fetches the Requests with `render` set, and those
    that name it. patchright and the browser start with the first such Request, so a
    Run that renders nothing never starts them; without `pernix[browser]` installed,
    or without the browser engine, that Request fails with a `ComponentError` saying
    how to install them.

    A Request without a Session gets a fresh browser context, closed after the
    fetch, so rendered Requests are independent as on the HTTP side (ADR 0011): a
    context holds only the Request's cookies, as host-only cookies of its host,
    Secure only for the names that need it (`__Secure-` and `__Host-`), and the
    cookies set while the page loads.

    A Request under a Session is loaded in a context kept for that Session, which
    one fetch uses at a time; concurrent fetches of a Session get one each. It is
    created with the Session's localStorage and the proxy of the Request or of the
    Session, an empty proxy for none, which never change afterwards: a Session whose
    proxy or localStorage changed elsewhere gets a new one. The browser cannot go
    through a SOCKS proxy with credentials. Before each fetch, the context is made to hold
    the Session's cookies, with the Request's own, and after it its pages are
    closed, those the page opened included, and the cookies the page changed, and
    the localStorage of the page's origin, are kept in the Session, whether the page
    loaded or not, but not when the fetch is cancelled. Attributes
    the browser does not tell, such as an unspecified SameSite or when a cookie was
    first set, are kept from the Session's cookie jar; a cookie the page made without
    SameSite is held as Lax, as the browser reports it. The localStorage of frames
    from other sites, which the browser partitions, is not kept. At most
    `browser.max_idle_contexts` contexts are kept idle between fetches, and one is
    closed once idle for `browser.context_idle` seconds; a Retired Session is
    never leased again, so its context goes that way too.

    At most `browser.max_pages` pages are open at once, each that of one fetch,
    which waits for one to close before it opens its own; `browser.deadline` then
    limits the whole fetch. The page is loaded until its `load` event, within
    `browser.timeout` (or `request.timeout`). A `Render` in `request.render` then
    waits for its `wait_for`, runs its actions, and takes its screenshot, each
    within `browser.action_timeout` unless an action sets its own. The page may not
    load the resource types of its `block`, or of `browser.block` without one, nor
    register service workers then, and the XHR and fetch responses matching its
    `capture` are kept; `response.browser` holds what they gave, with the document
    as received. When an action leads to another document, the Response describes
    that one.

    A Request other than a plain GET, or with headers, changes the first
    request of the page accordingly. The browser keeps those headers on the
    redirects that follow, to any host, as the HTTP Fetcher does, so it refuses
    `Authorization` and `Proxy-Authorization`, which the HTTP Fetcher drops on the
    way to another host; the method and body follow the browser's redirect rules,
    and the page's own requests are untouched.

    The body of an HTML or XHTML document is its rendered DOM, encoded as UTF-8;
    that of any other document, which Chrome would show in a viewer page of its own,
    is the body received. Headers are those of the document as the browser reports
    them, every value kept but not in wire order; `history` holds the redirects of
    the navigation.

    Under a network guard (`pernix.fetchers.network_guard`), as in tests, a browser
    launched while it is on reaches only loopback hosts, or any host through a
    loopback proxy of the Request's own: everything else, its own background
    requests included, goes to a proxy that refuses it. Requests a page makes to
    other hosts are recorded in the guard; a refused document raises
    `NetworkBlocked`.

    In `stats`, if given, `browser.pages` and `browser.contexts` count the pages and
    the contexts opened, `browser.pages_open` and `browser.contexts_open` tell how
    many are open, and `browser.page_waits` and `browser.page_wait_seconds` count the
    fetches that waited for a page, and how long.

    Attributes:
        schemes: None: the browser Fetcher renders only Requests that need it.
        settings_sections: Registers `BrowserSettings`.
    """

    schemes: ClassVar[frozenset[str]] = frozenset()
    settings_sections: ClassVar[tuple[type[Section], ...]] = (BrowserSettings,)

    def __init__(
        self,
        settings: BrowserSettings | None = None,
        *,
        default_encoding: str = "utf-8",
        stats: Stats | None = None,
    ) -> None:
        """Configure the fetcher.

        Args:
            settings: The browser settings; the defaults when `None`.
            default_encoding: The `default_encoding` of the Responses whose body is
                not a rendered DOM.
            stats: Where the pages and the contexts are counted.
        """
        self._settings = settings or BrowserSettings()
        self._default_encoding = default_encoding
        self._stats = stats
        self._open = False
        self._launch: asyncio.Task[LaunchedBrowser] | None = None
        self._unavailable: ComponentError | None = None
        self._closing: dict[BrowserContext, asyncio.Task[None]] = {}
        """The contexts closing in tasks of their own, which closing the fetcher waits for."""
        self._pages = PageSlots(self._settings.max_pages, stats=stats)
        self._contexts_open = 0
        self._contexts = SessionContexts(
            kept=self._settings.max_idle_contexts,
            idle=self._settings.context_idle,
            new_context=self._session_context,
            discard=self._discard,
        )

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> Self:
        """Build the fetcher for a Run from its settings.

        Args:
            crawler: The Crawler of the Run.

        Returns:
            A fetcher with the `browser.*` settings, whose Responses use
            `response.default_encoding`, counting in the Run's stats.
        """
        return cls(
            crawler.settings.section(BrowserSettings),
            default_encoding=crawler.settings.section(ResponseSettings).default_encoding,
            stats=crawler.stats,
        )

    async def __aenter__(self) -> Self:
        self._open = True
        self._unavailable = None
        self._pages = PageSlots(self._settings.max_pages, stats=self._stats)  # waiters bind to this event loop
        return self

    async def __aexit__(self, *_exc: object) -> None:
        self._open = False
        launch, self._launch = self._launch, None
        self._contexts.close()
        try:
            if self._closing:
                await asyncio.wait(self._closing.values())
        finally:
            if launch is not None:
                await _close_launched(launch)

    async def fetch(self, request: Request, *, session: Session | None = None) -> Response:
        """Load a Request in the browser, once fewer than `browser.max_pages` pages are open.

        Args:
            request: The Request to load.
            session: The Session to load it under, in a context kept for it, whose
                cookies and localStorage the fetch updates; `None` for a fresh
                context.

        Returns:
            The Response, whatever its status code, naming the Session.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            pernix.errors.ComponentError: patchright or the browser engine is not
                installed.
            pernix.errors.NetworkBlocked: The network guard refused the document.
            pernix.errors.FetchError: The page did not load; a subclass names the kind
                of failure.
            pernix.errors.ActionError: A step of the Render failed; an
                `ActionTimeout` if it took too long.
            ValueError: The Request has an `Authorization` or `Proxy-Authorization`
                header, or goes through a SOCKS proxy with credentials.
        """
        if not self._open:
            raise RuntimeError("BrowserFetcher must be entered with 'async with' before fetching")
        credentials = sorted({name for name, _ in request.headers if name.lower() in _CREDENTIAL_HEADERS})
        if credentials:
            raise ValueError(
                f"{request!r} has a {' and '.join(credentials)} header, which the browser would send on to any host a "
                "redirect leads to; render it without, or fetch it over HTTP"
            )
        proxy = request_proxy(request, session)
        if proxy is not None and _socks_with_credentials(proxy):
            raise ValueError(
                f"{request!r} goes through a SOCKS proxy with credentials, which the browser cannot use; "
                "render it through an HTTP proxy, or fetch it over HTTP"
            )
        guard = current_guard()
        if guard is not None:
            guard.check(request.url, proxy=proxy)
        page = _Page(await self._pages.take())
        try:
            async with self._deadline(request):
                try:
                    return await self._fetch(request, guard, proxy, session, page)
                except PernixError:
                    raise
                except Exception as exc:  # the browser's own errors
                    raise _fetch_error(exc, request) from exc
        finally:
            self._release(page)

    async def _fetch(
        self, request: Request, guard: NetworkGuard | None, proxy: str | None, session: Session | None, page: _Page
    ) -> Response:
        launched = await self._browser()
        if guard is not None and not launched.guarded:
            guard.blocked.append(request.url)
            raise NetworkBlocked(
                f"the network guard refused {request.url}: the browser was launched while the network was open, "
                "so it cannot be kept on loopback hosts"
            )
        started = time.monotonic()
        render = request.render if isinstance(request.render, Render) else Render()
        blocked = self._settings.block if render.block is None else render.block
        load = _Load(request, render, blocked, proxy=proxy, guarded=launched.guarded, started=started)
        workers = bool(blocked)  # service workers would load what is blocked, unseen by the page's routes
        if session is None:
            context = page.context = self._opened(
                await launched.new_context(proxy=proxy, blocks_service_workers=workers)
            )
            try:
                return await self._load(context, load)
            finally:
                await self._close_soon(context)
        async with self._contexts.using(launched, session, proxy=proxy, blocks_service_workers=workers) as kept:
            page.context = kept.context
            return await self._load(kept.context, load, kept=kept, session=session)

    async def _browser(self) -> LaunchedBrowser:
        """The browser, launched by the first fetch that needs it while the others wait."""
        if self._unavailable is not None:
            raise ComponentError(*self._unavailable.args) from self._unavailable.__cause__
        if self._launch is None:
            self._launch = asyncio.create_task(launch_browser(self._settings))
        launch = self._launch
        try:
            return await asyncio.shield(launch)  # a fetch that gives up waiting leaves the launch to the others
        except ComponentError as exc:
            self._unavailable = exc  # installing the extra or the engine takes a new Run
            raise
        except Exception:
            if self._launch is launch:
                self._launch = None  # a later fetch launches again
            raise

    async def _load(
        self,
        context: BrowserContext,
        load: _Load,
        *,
        kept: SessionContext | None = None,
        session: Session | None = None,
    ) -> Response:
        """Load a Request in a page of its own, keeping what it changed in its Session, if any."""
        page = await context.new_page()
        if self._stats is not None:
            self._stats.inc("browser.pages")
        watch = _PageLoad(page, proxy=load.proxy, guarded=load.guarded)
        try:
            response = await self._loaded(page, watch, load, kept, session)
        except asyncio.CancelledError:
            watch.stop()
            raise  # a kept context is closed, with the page
        except BaseException:
            await self._done(page, watch, kept, session)
            raise
        await self._done(page, watch, kept, session)
        return response

    async def _loaded(
        self, page: Page, watch: _PageLoad, load: _Load, kept: SessionContext | None, session: Session | None
    ) -> Response:
        request = load.request
        own = _own_cookies(request)
        if kept is not None and session is not None:
            await kept.sync(session, own, now=time.time())
        elif own:
            await page.context.add_cookies(own)
        if load.blocked:
            await page.route("**/*", ResourceBlocker(load.blocked))
        rewrite = _FirstRequest.of(page, request)
        if rewrite is not None:
            await page.route("**/*", rewrite)  # tried first, as the last added
        capture = ResponseCapture(page, load.render.capture) if load.render.capture else None
        try:
            return await self._rendered(page, watch, load, session, capture)
        finally:
            if capture is not None:
                capture.stop()

    async def _rendered(
        self, page: Page, watch: _PageLoad, load: _Load, session: Session | None, capture: ResponseCapture | None
    ) -> Response:
        """Load the page, then run the steps of its Render on it."""
        request = load.request
        timeout = request.timeout or self._settings.timeout
        try:
            document = await page.goto(request.url, wait_until="load", timeout=timeout * 1000)
        except Exception as exc:
            return await self._failed_load(exc, watch, load, session)
        if document is None:
            raise FetchError(f"loading {request.url} gave no document")
        action_timeout = self._settings.action_timeout
        try:
            outputs = await run_render(page, load.render, action_timeout=action_timeout, url=request.url)
        except ActionError as exc:
            failure = _navigation_failure(watch, request)
            if failure is not None:
                raise failure from exc  # the action failed as the page it acted on did not load
            raise
        captured = () if capture is None else await capture.responses(within=action_timeout)
        failure = _navigation_failure(watch, request)  # once nothing is awaited but the page itself
        if failure is not None:
            raise failure
        document = watch.shown or document  # an action may have led to another one
        result = RenderResult(
            document=await captured_response(document),
            captured=captured,
            screenshots=outputs.screenshots,
            evaluated=outputs.evaluated,
        )
        return await self._response(page, document, load, session, result)

    async def _done(self, page: Page, watch: _PageLoad, kept: SessionContext | None, session: Session | None) -> None:
        """End a fetch: in a kept context, close its pages and keep what it changed in its Session."""
        watch.stop()
        if kept is None or session is None:
            return  # its context is closed, with the pages
        try:
            await kept.merge(page, session, now=time.time())
        except Exception:
            kept.broken = True
            logger.warning(
                "Keeping what a rendered fetch changed in the Session %r failed; its browser context is closed",
                session.id,
                exc_info=True,
            )

    async def _failed_load(self, error: Exception, watch: _PageLoad, load: _Load, session: Session | None) -> Response:
        """Turn a page that did not load into the Response it still had, or the error it is."""
        from patchright.async_api import TimeoutError as PageTimeout  # imported once a browser runs

        request = load.request
        if watch.refused is not None:
            raise _refused(watch.refused, request) from None
        if isinstance(error, PageTimeout):
            seconds = request.timeout or self._settings.timeout
            raise Timeout(f"{request.url} did not load within {seconds} s") from None
        document = watch.document
        if document is not None and document.status in _NO_CONTENT:
            result = RenderResult(document=await captured_response(document, body=b""))
            return await self._built(
                load, watch.page, document, url=document.url, body=b"", session=session, result=result
            )
        if "Download is starting" in str(error):
            raise FetchError(f"the browser downloads {request.url} instead of showing it; fetch it without render")
        raise _fetch_error(error, request) from error

    async def _response(
        self, page: Page, document: PageResponse, load: _Load, session: Session | None, result: RenderResult
    ) -> Response:
        if await page.evaluate("document.contentType") in _RENDERED_TYPES:
            body = (await page.content()).encode()
            return await self._built(
                load, page, document, url=page.url, body=body, session=session, result=result, rendered=True
            )
        body = await document.body() if result.document is None else result.document.body
        return await self._built(load, page, document, url=document.url, body=body, session=session, result=result)

    async def _built(
        self,
        load: _Load,
        page: Page,
        document: PageResponse,
        *,
        url: str,
        body: bytes,
        session: Session | None,
        result: RenderResult,
        rendered: bool = False,
    ) -> Response:
        headers = body_headers(await _headers(document), re_encoded=rendered)
        history = await _history(document)
        held = await page.context.cookies([*(hop.url for hop in history), url])
        address = await document.server_addr()
        return Response(
            url=url,
            status=document.status,
            headers=headers,
            body=body,
            request=load.request,
            cookies={cookie.get("name", ""): cookie.get("value", "") for cookie in held},
            encoding="utf-8" if rendered else None,
            default_encoding=self._default_encoding,
            history=history,
            elapsed=time.monotonic() - load.started,
            ip_address=address["ipAddress"] if address else None,
            fetcher="browser",
            session=None if session is None else session.id,
            browser=result,
        )

    @asynccontextmanager
    async def _deadline(self, request: Request) -> AsyncGenerator[None]:
        """Limit a fetch to `browser.deadline`, as a `Timeout`."""
        deadline = self._settings.deadline
        scope = asyncio.timeout(deadline)
        try:
            async with scope:
                yield
        except TimeoutError:
            if scope.expired():
                raise Timeout(f"fetching {request.url} took longer than browser.deadline ({deadline} s)") from None
            raise

    async def _close_soon(self, context: BrowserContext) -> None:
        """Close a context in a task of its own, which a cancelled fetch leaves running and closing the fetcher waits for."""
        await asyncio.shield(self._discard(context))

    def _discard(self, context: BrowserContext) -> asyncio.Task[None]:
        """Start closing a context in a task of its own, which closing the fetcher waits for; once is enough."""
        closing = self._closing.get(context)
        if closing is None:
            closing = asyncio.create_task(_quietly(context.close(), "closing a browser context"))
            self._closing[context] = closing
            closing.add_done_callback(lambda _closing: self._closed(context))
        return closing

    def _release(self, page: _Page) -> None:
        """Give a fetch's page slot back once its page is closed: at once, or once its context, still closing, is."""
        closing = None if page.context is None else self._closing.get(page.context)
        if closing is None:
            page.slot.release()
        else:  # a fetch cancelled, or past browser.deadline, leaves its page open while its context closes
            closing.add_done_callback(lambda _closing: page.slot.release())

    async def _session_context(
        self, launched: LaunchedBrowser, session: Session, proxy: str | None, blocks_service_workers: bool
    ) -> BrowserContext:
        """A context for a Session, with its localStorage, a proxy, and service workers blocked or not."""
        context = await launched.new_context(
            proxy=proxy, local_storage=session.local_storage, blocks_service_workers=blocks_service_workers
        )
        return self._opened(context)

    def _opened(self, context: BrowserContext) -> BrowserContext:
        """Count a context created; every context is closed with `_discard()`, which counts it closed."""
        if self._stats is not None:
            self._stats.inc("browser.contexts")
        self._count_open(1)
        return context

    def _closed(self, context: BrowserContext) -> None:
        del self._closing[context]
        self._count_open(-1)

    def _count_open(self, change: int) -> None:
        self._contexts_open += change
        if self._stats is not None:
            self._stats.set("browser.contexts_open", self._contexts_open)


@dataclass(slots=True)
class _Page:
    """The page slot a fetch holds, and the context it opens its page in, once it has one."""

    slot: PageSlot
    context: BrowserContext | None = None


@dataclass(frozen=True, slots=True)
class _Load:
    """A Request on its way to the browser, with its Render options, the resource types it blocks, and its proxy."""

    request: Request
    render: Render
    blocked: tuple[str, ...]
    proxy: str | None
    guarded: bool
    started: float


class _PageLoad:
    """What the Fetcher watches while a page loads: its document, and the requests the network guard refused.

    Attributes:
        page: The page.
        document: The latest response to a navigation of the main frame, whether
            it shows a document or not, as a 204 does not.
        shown: The response of the document the main frame shows, once one was
            committed.
        failed: The navigation of the main frame that did not load since the
            last document was committed, which then shows an error page; one
            aborted, as by another navigation or a download, does not count.
        refused: The URL of the main frame's navigation that a guarded browser
            refused, if any.
    """

    def __init__(self, page: Page, *, proxy: str | None, guarded: bool) -> None:
        self.page = page
        self.document: PageResponse | None = None
        self.shown: PageResponse | None = None
        self.failed: PageRequest | None = None
        self.refused: str | None = None
        self._arrived: PageResponse | None = None
        """The navigation response the main frame has not committed yet."""
        self._proxy = proxy
        self._guarded = guarded
        page.on("response", self._on_response)
        page.on("framenavigated", self._on_navigated)
        page.on("requestfailed", self._on_failed)
        if guarded:
            page.context.on("request", self._on_request)
            page.on("websocket", self._on_websocket)

    def stop(self) -> None:
        """Stop watching, as the context may outlive the page."""
        if self._guarded:
            page_context = self.page.context
            page_context.remove_listener("request", self._on_request)

    def _on_response(self, response: PageResponse) -> None:
        if response.request.is_navigation_request() and response.frame == self.page.main_frame:
            self.document = response
            self._arrived = None if response.status in _NO_CONTENT else response  # those never commit

    def _on_navigated(self, frame: Frame) -> None:
        """Take the navigation response the main frame committed to; a 204 or a download commits none.

        A navigation within the document, such as `history.pushState()`, commits
        no response either, and its URL is not that of the response pending.
        """
        arrived = self._arrived
        if (
            frame == self.page.main_frame
            and arrived is not None
            and urldefrag(frame.url)[0] == urldefrag(arrived.url)[0]
        ):
            self.shown, self._arrived, self.failed = arrived, None, None

    def _on_failed(self, request: PageRequest) -> None:
        if not (request.is_navigation_request() and request.frame == self.page.main_frame):
            return
        if self._arrived is not None and self._arrived.request == request:
            self._arrived = None  # its response will not commit, as for a download
        found = _NET_ERROR.search(request.failure or "")
        if found is None or found.group(1) != "ERR_ABORTED":
            self.failed = request

    def _on_request(self, request: PageRequest) -> None:
        if self._refuses(request.url) and request.is_navigation_request() and request.frame == self.page.main_frame:
            self.refused = request.url

    def _on_websocket(self, websocket: WebSocket) -> None:
        self._refuses(websocket.url)

    def _refuses(self, url: str) -> bool:
        """Record a URL the refusing proxy takes, in the guard in effect."""
        if urlsplit(url).scheme not in _NETWORK_SCHEMES or admitted(url, proxy=self._proxy):
            return False
        guard = current_guard()
        if guard is not None:
            guard.blocked.append(url)
        return True


class _FirstRequest:
    """Sends the first request of a page with the method, body, and headers of the Request."""

    def __init__(self, page: Page, request: Request, headers: list[tuple[str, str]]) -> None:
        self._page = page
        self._request = request
        self._headers = headers
        self._pending = True

    @classmethod
    def of(cls, page: Page, request: Request) -> _FirstRequest | None:
        """The rewrite a Request needs, or `None` for a plain GET without headers.

        Args:
            page: The page that loads the Request.
            request: The Request.

        Returns:
            A route handler for the page, or `None`.
        """
        headers = [(name, value) for name, value in request.headers if name.lower() != "cookie"]
        if request.method == "GET" and request.body is None and not headers:
            return None
        return cls(page, request, headers)

    async def __call__(self, route: Route) -> None:
        sent = route.request
        if not (self._pending and sent.is_navigation_request() and sent.frame == self._page.main_frame):
            await route.fallback()
            return
        self._pending = False
        await route.continue_(
            method=self._request.method,
            post_data=self._request.body,
            headers=_merged(await sent.headers_array(), self._headers),
        )


def _merged(browser_headers: Iterable[NameValue], own: list[tuple[str, str]]) -> dict[str, str]:
    """The browser's headers with the Request's in place of those of the same names, keeping each name's case."""
    replaced = {name.lower() for name, _ in own}
    merged = {header["name"]: header["value"] for header in browser_headers if header["name"].lower() not in replaced}
    for name, value in own:
        merged[name] = f"{merged[name]}, {value}" if name in merged else value
    return merged


def _own_cookies(request: Request) -> list[SetCookieParam]:
    """The cookies a Request carries, held for its host only, for every path, sent in the order given."""
    cookies = request_cookies(request)
    host = url_host(request.url) or urlsplit(request.url).hostname or ""
    return [request_cookie(host, name, value) for name, value in cookies.items()] if cookies else []


async def _headers(response: PageResponse) -> Headers:
    return Headers((header["name"], header["value"]) for header in await response.headers_array())


async def _history(document: PageResponse) -> list[Redirect]:
    """The redirects that led to a document, oldest first."""
    hops: list[Redirect] = []
    hop = document.request.redirected_from
    while hop is not None:
        response = await hop.response()
        if response is not None:
            hops.append(Redirect(hop.url, response.status, await _headers(response)))
        hop = hop.redirected_from
    return hops[::-1]


def _fetch_error(error: Exception, request: Request) -> FetchError:
    """Map a browser error onto the FetchError of its kind."""
    message = str(error).splitlines()[0] if str(error) else type(error).__name__
    return _error_kind(message)(f"loading {request.url} failed: {message}")


def _navigation_failure(watch: _PageLoad, request: Request) -> FetchError | None:
    """The error of a navigation of the main frame, after the page had loaded, that was refused or did not load."""
    if watch.refused is not None:
        return _refused(watch.refused, request)
    failed = watch.failed
    if failed is None:
        return None
    failure = failed.failure or "the browser tells no reason"
    return _error_kind(failure)(f"the page of {request.url} navigated to {failed.url}, which did not load: {failure}")


def _error_kind(message: str) -> type[FetchError]:
    """The FetchError of the Chrome network error a message names; `FetchError` for none."""
    found = _NET_ERROR.search(message)
    name = found.group(1) if found else ""
    return next((kind for prefix, kind in _NET_ERROR_KINDS if name.startswith(prefix)), FetchError)


def _refused(url: str, request: Request) -> NetworkBlocked:
    return NetworkBlocked(
        f"the network guard refused {url}, on the way to {request.url}: only loopback hosts, "
        "such as 127.0.0.1 and localhost, may be reached while it is on"
    )


async def _quietly(closing: Awaitable[None], what: str) -> None:
    try:
        await closing
    except Exception:  # a browser that went away closed its contexts with it
        logger.debug("Ignoring an error %s", what, exc_info=True)


async def _close_launched(launch: asyncio.Task[LaunchedBrowser]) -> None:
    """Close a launched browser, or stop a launch in progress."""
    if not launch.done():
        launch.cancel()
    await asyncio.wait([launch])
    if launch.cancelled() or launch.exception() is not None:
        return  # a launch that failed or was stopped cleaned up after itself
    await launch.result().close()


def _socks_with_credentials(proxy: str) -> bool:
    """Whether a proxy is a SOCKS one with credentials, which Chrome does not support."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    return parts.scheme.lower().startswith("socks") and "@" in parts.netloc
