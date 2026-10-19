"""The browser Fetcher when its renderer, its browser, or patchright's driver dies, against the loopback server."""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import TYPE_CHECKING

import psutil
import pytest

from browser_kills import HELD_PAGE, HTML, Held, browsers, descendants, drivers, gone, kill
from pernix import Request, Session
from pernix.errors import BrowserLost, PageCrashed
from pernix.fetchers import BrowserFetcher, BrowserSettings, browser_generations
from pernix.stats import Stats
from pernix.testing import ReceivedRequest, Reply

if TYPE_CHECKING:
    from patchright.async_api import Page

    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")


def _render(url: str) -> Request:
    return Request(url, render=True)


@pytest.fixture
def held(server: LoopbackServer) -> Held:
    """The server holds the image of `/held`, so its page is loading, and serves `/` at once."""
    handler = Held()
    server.route("/held", HELD_PAGE, headers=HTML)
    server.route("/held.png", handler=handler)
    server.route("/", "<p>up</p>", headers=HTML)
    return handler


@pytest.mark.parametrize("session", [None, Session("alice")])
async def test_a_page_whose_renderer_dies_fails_with_page_crashed(
    server: LoopbackServer, held: Held, session: Session | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_generations, "PROBE_INTERVAL", 3600.0)  # a slow runner's browser is not the subject
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        loading = asyncio.create_task(fetcher.fetch(_render(server.url("/held")), session=session))
        await held.wait()
        page = _page_loading(fetcher, "/held")
        devtools = await page.context.new_cdp_session(page)
        crashing = asyncio.create_task(devtools.send("Page.crash"))  # its own renderer, which never answers
        with pytest.raises(PageCrashed, match=r"the page of http://127\.0\.0\.1:\d+/held crashed"):
            await loading
        crashing.cancel()
        held.release.set()
        response = await fetcher.fetch(_render(server.url("/")), session=session)  # in the same browser

    assert response.css("p::text").get() == "up"
    assert stats.get("browser.page_crashes") == 1
    assert stats.get("browser.generations") == 1
    assert stats.get("browser.contexts_open") == 0


async def test_a_browser_that_dies_fails_its_fetches_with_browser_lost_and_the_next_starts_another(
    server: LoopbackServer, held: Held
) -> None:
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        loading = asyncio.create_task(fetcher.fetch(_render(server.url("/held"))))
        await held.wait()
        first = descendants()
        kill(browsers())
        with pytest.raises(BrowserLost, match="the browser disconnected"):
            await loading
        held.release.set()
        response = await fetcher.fetch(_render(server.url("/")))
        left = await gone(first)

    assert response.css("p::text").get() == "up"
    assert left == []
    assert (stats.get("browser.lost_by_reason.disconnected"), stats.get("browser.generations")) == (1, 2)


async def test_a_driver_that_dies_takes_its_browser_with_it(server: LoopbackServer, held: Held) -> None:
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        loading = asyncio.create_task(fetcher.fetch(_render(server.url("/held"))))
        await held.wait()
        first = descendants()
        kill(drivers())
        with pytest.raises(BrowserLost, match="patchright's driver died"):
            await loading
        held.release.set()
        left = await gone(first)  # the browser outlives its driver, until it is killed
        response = await fetcher.fetch(_render(server.url("/")))

    assert left == []
    assert response.css("p::text").get() == "up"
    assert (stats.get("browser.lost_by_reason.driver"), stats.get("browser.generations")) == (1, 2)


async def test_a_browser_that_stops_answering_is_killed_with_its_driver(
    server: LoopbackServer, held: Held, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(browser_generations, "PROBE_INTERVAL", 0.2)
    monkeypatch.setattr(browser_generations, "PROBE_TIMEOUT", 0.5)
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        loading = asyncio.create_task(fetcher.fetch(_render(server.url("/held"))))
        await held.wait()
        first = descendants()
        [browser] = browsers()
        os.kill(browser.pid, signal.SIGSTOP)  # its driver waits for it, as for one that hangs
        with pytest.raises(BrowserLost, match="the browser stopped answering"):
            await loading
        held.release.set()
        left = await gone(first)
        response = await fetcher.fetch(_render(server.url("/")))

    assert left == []
    assert response.css("p::text").get() == "up"
    assert stats.get("browser.lost_by_reason.unresponsive") == 1


async def test_a_browser_replaced_under_load_finishes_its_pages_and_leaves_no_process(server: LoopbackServer) -> None:
    server.route("/slow", "<p>slow</p><img src='/slow.png'>", headers=HTML)
    server.route("/slow.png", handler=_slowly)
    stats = Stats()
    seen: set[int] = set()
    settings = BrowserSettings(max_pages=2, generation_pages=2)

    async with BrowserFetcher(settings, stats=stats) as fetcher:
        fetches = [asyncio.create_task(fetcher.fetch(_render(server.url(f"/slow?n={n}")))) for n in range(6)]
        while not all(fetch.done() for fetch in fetches):
            seen.update(process.pid for process in descendants())
            await asyncio.sleep(0.05)
        responses = [fetch.result() for fetch in fetches]
        assert await _until_one_browser() == 1  # the replaced ones closed once their pages were done
        processes = descendants()

    assert [response.css("p::text").get() for response in responses] == ["slow"] * 6
    assert (stats.get("browser.generations"), stats.get("browser.replaced_by_reason.pages")) == (3, 2)
    assert await gone(processes) == []
    assert [pid for pid in seen if _running(pid)] == []


async def test_a_browser_over_its_memory_is_replaced(server: LoopbackServer, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(browser_generations, "PROBE_INTERVAL", 0.1)
    server.route("/", "<p>up</p>", headers=HTML)
    stats = Stats()

    async with BrowserFetcher(BrowserSettings(generation_memory=1), stats=stats) as fetcher:
        await fetcher.fetch(_render(server.url("/")))
        async with asyncio.timeout(10):
            while stats.get("browser.replaced_by_reason.memory") is None:  # noqa: ASYNC110  # the probe replaces it
                await asyncio.sleep(0.05)
        await fetcher.fetch(_render(server.url("/")))

    assert stats.get("browser.generations") == 2


def _page_loading(fetcher: BrowserFetcher, path: str) -> Page:
    """The page of the fetcher's browser that is loading a path."""
    generation = fetcher._generations._current  # pyright: ignore[reportPrivateUsage]
    assert generation is not None
    pages = [page for context in generation.launched.browser.contexts for page in context.pages]
    [page] = [page for page in pages if page.url.endswith(path)]
    return page


def _slowly(_request: ReceivedRequest) -> Reply:
    time.sleep(0.3)
    return Reply(b"", headers=[("Content-Type", "image/png")])


async def _until_one_browser() -> int:
    """Wait until one browser runs, and return how many do then."""
    async with asyncio.timeout(10):
        while len(browsers()) > 1:  # noqa: ASYNC110  # processes tell nothing when they end
            await asyncio.sleep(0.05)
    return len(browsers())


def _running(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False
