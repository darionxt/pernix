"""How many pages the browser Fetcher opens at once, against the loopback server, and the stats it keeps of them."""

from __future__ import annotations

import asyncio
import threading
import time
from typing import TYPE_CHECKING

import pytest

from pernix import Request, Session
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.stats import Stats
from pernix.testing import ReceivedRequest, Reply

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]


class SlowPages:
    """A handler answering after a pause, recording how many requests it answers at once."""

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds
        self.most = 0
        self.arrived = threading.Event()
        self._active = 0
        self._lock = threading.Lock()

    def __call__(self, _request: ReceivedRequest) -> Reply:
        with self._lock:
            self._active += 1
            self.most = max(self.most, self._active)
        self.arrived.set()
        try:
            time.sleep(self.seconds)
        finally:
            with self._lock:
                self._active -= 1
        return Reply("<p>slow</p>", headers=HTML)


def _render(url: str) -> Request:
    return Request(url, render=True)


async def test_at_most_browser_max_pages_pages_are_open_at_once(server: LoopbackServer) -> None:
    slow = SlowPages(0.3)
    server.route("/slow", handler=slow)
    stats = Stats()

    async with BrowserFetcher(BrowserSettings(max_pages=2), stats=stats) as fetcher:
        responses = await asyncio.gather(*(fetcher.fetch(_render(server.url(f"/slow?n={n}"))) for n in range(5)))

    assert [response.css("p::text").get() for response in responses] == ["slow"] * 5
    assert slow.most == 2
    assert stats.get("browser.page_waits") == 3
    assert stats.get("browser.pages") == 5
    assert stats.get("browser.pages_open") == 0


async def test_waiting_for_a_page_takes_no_part_in_the_deadline(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)
    server.route("/slow", handler=SlowPages(1.0))
    settings = BrowserSettings(max_pages=1, deadline=2.0)

    async with BrowserFetcher(settings) as fetcher:
        await fetcher.fetch(_render(server.url("/")))  # the browser starts, which the deadline counts
        responses = await asyncio.gather(*(fetcher.fetch(_render(server.url(f"/slow?n={n}"))) for n in range(2)))

    assert [response.status for response in responses] == [200, 200]  # the second waited a second first


async def test_a_page_whose_fetch_was_cancelled_holds_its_slot_until_its_context_is_closed(
    server: LoopbackServer,
) -> None:
    slow = SlowPages(2.0)
    server.route("/slow", handler=slow)
    server.route("/", "<p>up</p>", headers=HTML)
    stats = Stats()

    async with BrowserFetcher(BrowserSettings(max_pages=1), stats=stats) as fetcher:
        loading = asyncio.create_task(fetcher.fetch(_render(server.url("/slow")), session=Session("alice")))
        assert await asyncio.to_thread(slow.arrived.wait, 30)
        loading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loading

        assert stats.get("browser.pages_open") == 1  # its context, and the page in it, are still closing
        response = await fetcher.fetch(_render(server.url("/")), session=Session("bob"))

    assert response.status == 200
    assert stats.get("browser.pages_open") == 0
    assert stats.get("browser.page_waits") == 1


async def test_the_contexts_opened_and_open_are_counted(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        await fetcher.fetch(_render(server.url("/")))
        assert stats.get("browser.contexts_open") == 0  # a context of its own, closed after the fetch
        await fetcher.fetch(_render(server.url("/")), session=Session("alice"))
        await fetcher.fetch(_render(server.url("/")), session=Session("alice"))
        assert stats.get("browser.contexts_open") == 1  # kept for the Session between its fetches

    assert stats.get("browser.contexts") == 2
    assert stats.get("browser.contexts_open") == 0
    assert stats.get("browser.pages") == 3
