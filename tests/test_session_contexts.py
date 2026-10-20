"""The browser contexts kept for Sessions, against fake contexts that hold cookies as a browser reports them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, cast

import pytest

from pernix import Cookie, Session
from pernix.fetchers import session_contexts
from pernix.fetchers.browser_cookies import request_cookie
from pernix.fetchers.session_contexts import SessionContexts

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Page

    from pernix.fetchers.launched_browser import LaunchedBrowser

NOW = 1_800_000_000.0
ORIGIN = "https://shop.example"


class FakeContext:
    """Holds cookies as the browser reports them; an expired one added removes the one it would replace."""

    def __init__(self, proxy: str | None, local_storage: dict[str, dict[str, str]]) -> None:
        self.proxy = proxy
        self.local_storage = local_storage
        self.held: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.added: list[list[dict[str, Any]]] = []
        self.pages: list[Any] = []

    async def cookies(self) -> list[dict[str, Any]]:
        return [dict(cookie) for cookie in self.held.values()]

    async def add_cookies(self, cookies: list[dict[str, Any]]) -> None:
        self.added.append(cookies)
        for cookie in cookies:
            key = (cookie["name"], cookie["domain"], cookie["path"])
            if cookie.get("expires") == 0:
                self.held.pop(key, None)
            else:
                self.held[key] = {"expires": -1, "httpOnly": False, "secure": False, "sameSite": "Lax", **cookie}


class FakePage:
    def __init__(self, origin: str, storage: dict[str, str]) -> None:
        self.origin = origin
        self.storage = storage

    async def evaluate(self, _expression: str) -> list[Any]:
        return [self.origin, list(self.storage.items())]


class Browser:
    """Creates fake contexts, and records those closed."""

    def __init__(self) -> None:
        self.created: list[FakeContext] = []
        self.blocking: list[bool] = []
        self.closed: list[FakeContext] = []

    async def new_context(
        self, _launched: LaunchedBrowser, session: Session, proxy: str | None, blocks_service_workers: bool
    ) -> BrowserContext:
        context = FakeContext(proxy, {origin: dict(items) for origin, items in session.local_storage.items()})
        self.created.append(context)
        self.blocking.append(blocks_service_workers)
        return cast("BrowserContext", context)

    def discard(self, context: BrowserContext) -> None:
        self.closed.append(cast("FakeContext", context))


LAUNCHED = cast("LaunchedBrowser", object())


def _contexts(browser: Browser, *, kept: int = 8, idle: float = 300.0) -> SessionContexts:
    return SessionContexts(kept=kept, idle=idle, new_context=browser.new_context, discard=browser.discard)


async def _fetch(
    contexts: SessionContexts, session: Session, *, proxy: str | None = None, blocks_service_workers: bool = False
) -> FakeContext:
    async with contexts.using(LAUNCHED, session, proxy=proxy, blocks_service_workers=blocks_service_workers) as held:
        return cast("FakeContext", held.context)


async def test_a_sessions_context_is_kept_for_its_next_fetch_and_others_get_their_own() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    alice, bob = Session("alice"), Session("bob")

    first = await _fetch(contexts, alice)
    again = await _fetch(contexts, alice)
    other = await _fetch(contexts, bob)

    assert first is again
    assert other is not first
    assert (len(browser.created), browser.closed, len(contexts)) == (2, [], 2)


async def test_concurrent_fetches_of_a_session_each_get_a_context() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice")

    async with (
        contexts.using(LAUNCHED, session, proxy=None) as first,
        contexts.using(LAUNCHED, session, proxy=None) as second,
    ):
        assert first.context is not second.context

    assert len(contexts) == 2


async def test_contexts_beyond_the_bound_are_closed_least_recently_used_first() -> None:
    browser = Browser()
    contexts = _contexts(browser, kept=1)

    alice = await _fetch(contexts, Session("alice"))
    bob = await _fetch(contexts, Session("bob"))

    assert browser.closed == [alice]
    assert await _fetch(contexts, Session("bob")) is bob


async def test_without_room_each_context_is_closed_after_its_fetch() -> None:
    browser = Browser()
    contexts = _contexts(browser, kept=0)

    context = await _fetch(contexts, Session("alice"))

    assert browser.closed == [context]


async def test_a_context_unused_for_the_idle_time_is_closed() -> None:
    browser = Browser()
    contexts = _contexts(browser, idle=0.05)

    context = await _fetch(contexts, Session("alice"))
    await asyncio.sleep(0.1)

    assert browser.closed == [context]
    assert len(contexts) == 0


@pytest.mark.parametrize("change", ["proxy", "local_storage"])
async def test_a_session_whose_proxy_or_storage_changed_gets_a_new_context(change: str) -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice")
    old = await _fetch(contexts, session, proxy="http://proxy-1:8080")

    if change == "local_storage":
        session.local_storage[ORIGIN] = {"set": "elsewhere"}
    new = await _fetch(contexts, session, proxy="http://proxy-2:8080" if change == "proxy" else "http://proxy-1:8080")

    assert new is not old
    assert browser.closed == [old]


async def test_a_fetch_blocking_service_workers_never_uses_a_context_that_let_them_register() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice")

    open_context = await _fetch(contexts, session)
    blocking = await _fetch(contexts, session, blocks_service_workers=True)
    blocking_again = await _fetch(contexts, session, blocks_service_workers=True)
    reopened = await _fetch(contexts, session)

    assert blocking is blocking_again
    assert len({id(open_context), id(blocking), id(reopened)}) == 3
    assert browser.blocking == [False, True, False]
    assert browser.closed == [open_context, blocking]


async def test_a_context_whose_fetch_was_cancelled_or_that_broke_is_closed() -> None:
    browser = Browser()
    contexts = _contexts(browser)

    with pytest.raises(asyncio.CancelledError):
        async with contexts.using(LAUNCHED, Session("alice"), proxy=None):
            raise asyncio.CancelledError
    async with contexts.using(LAUNCHED, Session("bob"), proxy=None) as broken:
        broken.broken = True
    with pytest.raises(ValueError, match="failed"):
        async with contexts.using(LAUNCHED, Session("carol"), proxy=None):
            raise ValueError("the page failed")

    assert len(browser.closed) == 2
    assert len(contexts) == 1  # a page that failed to load leaves its context usable


async def test_closing_closes_every_context_kept() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    await _fetch(contexts, Session("alice"))
    await _fetch(contexts, Session("bob"))

    contexts.close()

    assert len(browser.closed) == 2
    assert len(contexts) == 0


async def test_a_fetch_holds_the_sessions_cookies_and_the_requests_own_and_keeps_what_the_page_changed() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session(
        "alice",
        cookies=[
            Cookie(name="sid", value="1", domain="shop.example", created=NOW - 10, modified=NOW - 10),
            Cookie(name="gone", value="1", domain="shop.example", expires=NOW - 1),  # expired
        ],
    )

    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        context = cast("FakeContext", held.context)
        context.held[("stale", "shop.example", "/")] = {
            "name": "stale",
            "value": "1",
            "domain": "shop.example",
            "path": "/",
        }
        await held.sync(session, [request_cookie("shop.example", "own", "1")], now=NOW)
        assert sorted(name for name, _, _ in context.held) == ["own", "sid"]
        context.held[("sid", "shop.example", "/")]["value"] = "2"  # as the page changed it
        await held.merge(cast("Page", FakePage(ORIGIN, {"k": "v"})), session, now=NOW)

    sid, gone = session.cookies
    assert (sid.name, sid.value, sid.created, sid.modified) == ("sid", "2", NOW - 10, NOW)
    assert (gone.name, gone.value) == ("gone", "1")  # expired, so never in the browser, and left as it was
    assert session.local_storage == {ORIGIN: {"k": "v"}}
    assert held.local_storage == {ORIGIN: {"k": "v"}}
    assert await _fetch(contexts, session) is context  # its storage is the Session's


async def test_a_context_that_was_not_synced_changes_nothing_and_is_closed() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice")

    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        await held.merge(cast("Page", FakePage(ORIGIN, {"k": "v"})), session, now=NOW)

    assert session.local_storage == {}
    assert len(browser.closed) == 1


async def test_an_opaque_origin_keeps_no_storage() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice")

    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        await held.sync(session, [], now=NOW)
        await held.merge(cast("Page", FakePage("null", {"k": "v"})), session, now=NOW)

    assert session.local_storage == {}


async def test_a_cookie_the_page_set_over_a_requests_own_takes_the_attributes_the_browser_reports() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    stored = Cookie(
        name="sid",
        value="session",
        domain="shop.example",
        expires=NOW + 3600,
        secure=True,
        http_only=True,
        created=NOW - 10,
        modified=NOW - 10,
    )
    session = Session("alice", cookies=[stored])

    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        await held.sync(session, [request_cookie("shop.example", "sid", "own")], now=NOW)
        context = cast("FakeContext", held.context)
        context.held[("sid", "shop.example", "/")]["value"] = (
            "new"  # replaced, like the Request's: plain, for the session
        )
        await held.merge(cast("Page", FakePage(ORIGIN, {})), session, now=NOW)

    [sid] = session.cookies
    assert (sid.value, sid.secure, sid.http_only, sid.expires, sid.created) == ("new", False, False, None, NOW - 10)


async def test_a_fetch_cancelled_while_its_changes_are_read_keeps_none_of_them() -> None:
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice", cookies=[Cookie(name="sid", value="1", domain="shop.example")])
    reading = asyncio.Event()

    class Stuck:
        async def evaluate(self, _expression: str) -> list[Any]:
            reading.set()
            await asyncio.Event().wait()
            return []

    async def fetch() -> None:
        async with contexts.using(LAUNCHED, session, proxy=None) as held:
            await held.sync(session, [], now=NOW)
            cast("FakeContext", held.context).held[("sid", "shop.example", "/")]["value"] = "2"
            await held.merge(cast("Page", Stuck()), session, now=NOW)

    task = asyncio.create_task(fetch())
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert [cookie.value for cookie in session.cookies] == ["1"]
    assert len(browser.closed) == 1


async def test_a_context_is_taught_the_hints_it_does_not_keep_and_the_session_keeps_those_its_page_asked_for(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    taught: list[tuple[str, tuple[str, ...]]] = []
    failing = {"https://down.example"}

    async def teach(_context: object, origin: str, hints: tuple[str, ...]) -> None:
        if origin in failing:
            raise RuntimeError("the page did not open")
        taught.append((origin, tuple(hints)))

    monkeypatch.setattr(session_contexts, "teach_hints", teach)
    browser = Browser()
    contexts = _contexts(browser)
    session = Session("alice", client_hints={ORIGIN: ("sec-ch-ua-arch",), "https://down.example": ("dpr",)})

    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        await held.sync(session, [], now=NOW)
        accepted = [(f"{ORIGIN}/a", "Sec-CH-UA-Model"), ("http://insecure.example/", "dpr")]
        await held.merge(cast("Page", FakePage(ORIGIN, {})), session, now=NOW, accepted=accepted)
    failing.clear()
    async with contexts.using(LAUNCHED, session, proxy=None) as held:
        await held.sync(session, [], now=NOW)

    assert taught == [(ORIGIN, ("sec-ch-ua-arch",)), ("https://down.example", ("dpr",))]
    assert (
        session.client_hints
        == held.client_hints
        == {
            ORIGIN: ("sec-ch-ua-model",),
            "https://down.example": ("dpr",),
        }
    )
    assert "Teaching a browser context the client hints of https://down.example failed" in caplog.text
