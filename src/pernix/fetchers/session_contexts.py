"""Browser contexts kept for Sessions between their rendered fetches, each used by one fetch at a time."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pernix.fetchers.browser_cookies import apply_page_changes, browser_cookie, browser_key, changes, keyed

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable

    from patchright._impl._api_structures import Cookie as BrowserCookie
    from patchright._impl._api_structures import SetCookieParam
    from patchright.async_api import BrowserContext, Page

    from pernix.fetchers.browser_cookies import BrowserKey
    from pernix.fetchers.launched_browser import LaunchedBrowser
    from pernix.session import Session

__all__ = ["NewContext", "SessionContext", "SessionContexts"]

type NewContext = Callable[[LaunchedBrowser, Session, str | None], Awaitable[BrowserContext]]
"""Creates the context of a Session in a browser, with a proxy and the Session's localStorage."""

_LOCAL_STORAGE = "() => [location.origin, Object.entries(localStorage)]"


class SessionContext:
    """A browser context kept for a Session, which holds its cookies and the localStorage of its origins.

    Before each fetch, its cookies are made those of the Session, with the
    Request's own; after it, the changes the page made are merged back into the
    Session. Its proxy is fixed when it is created.

    Attributes:
        session: The id of the Session.
        proxy: The proxy of the context, if any.
        context: The browser context.
        launched: The browser it belongs to.
        local_storage: The localStorage the context holds, by origin, as last seen.
        broken: Whether it failed in a way that keeps it from being used again.
    """

    def __init__(
        self,
        session: Session,
        context: BrowserContext,
        *,
        proxy: str | None,
        launched: LaunchedBrowser,
    ) -> None:
        """Keep a context created for a Session, with the Session's localStorage.

        Args:
            session: The Session.
            context: The context.
            proxy: Its proxy.
            launched: The browser it belongs to.
        """
        self.session = session.id
        self.context = context
        self.proxy = proxy
        self.launched = launched
        self.local_storage = {origin: dict(items) for origin, items in session.local_storage.items()}
        self.broken = False
        self._cookies: dict[BrowserKey, BrowserCookie] | None = None
        """The cookies the context held once synced for the fetch in progress; `None` until then."""
        self._overrides: frozenset[BrowserKey] = frozenset()
        """The keys of the cookies it holds as the Request's own for the fetch in progress."""

    def fits(self, session: Session, *, proxy: str | None, launched: LaunchedBrowser) -> bool:
        """Whether a fetch under a Session can use the context: same browser and proxy, and the Session's localStorage.

        Args:
            session: The Session.
            proxy: The proxy of the fetch.
            launched: The browser of the fetch.

        Returns:
            Whether it fits; one whose localStorage changed elsewhere does not.
        """
        return (
            not self.broken
            and self.launched is launched
            and self.proxy == proxy
            and self.local_storage == session.local_storage
        )

    async def sync(self, session: Session, request_cookies: Iterable[SetCookieParam], *, now: float) -> None:
        """Make the context hold the cookies of a Session that have not expired, with the Request's own, and no other.

        Args:
            session: The Session.
            request_cookies: The Request's own cookies, from
                `pernix.fetchers.browser_cookies.request_cookie()`; they take the place
                of the Session's of the same key.
            now: The current time, in seconds since the epoch.
        """
        self._cookies = None
        own = [browser_cookie(cookie) for cookie in session.cookies if cookie.expires is None or cookie.expires > now]
        overrides = [*request_cookies]
        wanted = {browser_key(cookie): cookie for cookie in [*own, *overrides]}
        self._overrides = frozenset(browser_key(cookie) for cookie in overrides)
        held = keyed(await self.context.cookies())
        missing = changes(held, wanted)
        if missing:
            await self.context.add_cookies(missing)
            held = keyed(await self.context.cookies())
        self._cookies = held

    async def merge(self, page: Page, session: Session, *, now: float) -> None:
        """End a fetch: close its pages, and keep in its Session the cookies it changed and the localStorage of its page.

        What the fetch changed is read, and the pages of the context closed, the
        page's and those it opened, before any of it is kept in the Session, so a
        fetch cancelled meanwhile keeps nothing. A context that was not synced for
        the fetch, as syncing failed, changes nothing and is marked broken, as what it
        holds is not known; so is one whose pages cannot be closed.

        Args:
            page: The page of the fetch, still open.
            session: The Session.
            now: The time of the changes, in seconds since the epoch.
        """
        before, self._cookies = self._cookies, None
        if before is None:
            self.broken = True
            return
        after = keyed(await self.context.cookies())
        storage = await _local_storage(page)
        await self._close_pages()
        apply_page_changes(session.cookies, before, after, now=now, overrides=self._overrides)
        if storage is not None:
            self._keep_local_storage(session, *storage)

    async def _close_pages(self) -> None:
        """Close every page of the context: one fetch uses it at a time, so they are all its fetch's."""
        try:
            for page in list(self.context.pages):
                await page.close()
        except Exception:
            self.broken = True

    def _keep_local_storage(self, session: Session, origin: str, items: dict[str, str]) -> None:
        if items == self.local_storage.get(origin, {}):
            return
        if items:
            session.local_storage[origin] = items
            self.local_storage[origin] = dict(items)
        else:
            session.local_storage.pop(origin, None)
            self.local_storage.pop(origin, None)


class SessionContexts:
    """The browser contexts kept for Sessions: a fetch under a Session uses one of its own at a time.

    A fetch takes the context kept for its Session that fits it, or a new one,
    created with the Session's localStorage and the fetch's proxy, and gives it back
    once it ends. Concurrent fetches of one Session each get one. A Session's kept
    contexts that no longer fit, as its proxy or its localStorage changed, are
    closed. At most `kept` contexts are kept between fetches, the least recently used
    closed beyond that, and one kept for `idle` seconds is closed. A context whose
    fetch was cancelled, or that broke, is closed instead of kept.
    """

    def __init__(
        self,
        *,
        kept: int,
        idle: float,
        new_context: NewContext,
        discard: Callable[[BrowserContext], object],
    ) -> None:
        """Configure the contexts.

        Args:
            kept: Contexts kept between fetches at most.
            idle: Seconds a context is kept without a fetch.
            new_context: Creates the context of a Session.
            discard: Closes a context, without waiting for it.
        """
        self._kept = kept
        self._idle = idle
        self._new_context = new_context
        self._discard = discard
        self._waiting: list[SessionContext] = []  # least recently used first
        self._expiries: dict[SessionContext, asyncio.TimerHandle] = {}

    def __len__(self) -> int:
        return len(self._waiting)

    @asynccontextmanager
    async def using(
        self, launched: LaunchedBrowser, session: Session, *, proxy: str | None
    ) -> AsyncGenerator[SessionContext]:
        """Use a context of a Session for one fetch.

        Args:
            launched: The browser.
            session: The Session.
            proxy: The proxy of the fetch.

        Yields:
            The context, which no other fetch uses meanwhile.
        """
        held = self._take(launched, session, proxy)
        if held is None:
            context = await self._new_context(launched, session, proxy)
            held = SessionContext(session, context, proxy=proxy, launched=launched)
        try:
            yield held
        except asyncio.CancelledError:
            self._close(held)  # a page may still be loading in it
            raise
        except BaseException:
            self._give_back(held)
            raise
        else:
            self._give_back(held)

    def close(self) -> None:
        """Close every context kept, without waiting for them."""
        waiting, self._waiting = self._waiting, []
        for held in waiting:
            self._close(held)

    def _take(self, launched: LaunchedBrowser, session: Session, proxy: str | None) -> SessionContext | None:
        """The most recently used context kept for a Session that fits a fetch; the Session's others are closed."""
        mine = [held for held in self._waiting if held.session == session.id]
        fitting = [held for held in mine if held.fits(session, proxy=proxy, launched=launched)]
        taken = fitting[-1] if fitting else None
        for held in mine:
            if held is not taken and held not in fitting:
                self._waiting.remove(held)
                self._close(held)
        if taken is not None:
            self._waiting.remove(taken)
            self._expiries.pop(taken).cancel()
        return taken

    def _give_back(self, held: SessionContext) -> None:
        if held.broken or self._kept == 0:
            self._close(held)
            return
        self._waiting.append(held)
        self._expiries[held] = asyncio.get_running_loop().call_later(self._idle, self._expire, held)
        while len(self._waiting) > self._kept:
            self._close(self._waiting.pop(0))

    def _expire(self, held: SessionContext) -> None:
        if held in self._waiting:
            self._waiting.remove(held)
            self._close(held)

    def _close(self, held: SessionContext) -> None:
        expiry = self._expiries.pop(held, None)
        if expiry is not None:
            expiry.cancel()
        self._discard(held.context)


async def _local_storage(page: Page) -> tuple[str, dict[str, str]] | None:
    """The origin of a page and its localStorage; `None` for an opaque origin or a page that cannot tell."""
    try:
        origin, entries = await page.evaluate(_LOCAL_STORAGE)
    except Exception:  # a crashed or closed page, or one whose storage is denied
        return None
    if not isinstance(origin, str) or origin == "null":
        return None
    return origin, {str(name): str(value) for name, value in entries}
