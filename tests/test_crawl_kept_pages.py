"""Runs whose Callbacks work with the pages they keep, and whose rendered Inline Fetches run page methods."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from browser_kills import browsers, drivers, kill
from pernix import Request, Response, Spider
from pernix.browser import Render
from pernix.crawler import Crawler
from pernix.fetchers.browser_processes import BrowserProcesses
from pernix.testing import crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

    import psutil
    from patchright.async_api import Page

    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]

LISTING = """<button id="more" onclick="document.getElementById('hidden').hidden = false">more</button>
<p id="hidden" hidden>three more books</p><a id="next" href="/next">next</a>"""


@pytest.fixture
def site(server: LoopbackServer) -> str:
    server.route("/", LISTING, headers=HTML)
    server.route("/next", "<h1>page two</h1>", headers=HTML)
    return server.url("").rstrip("/")


class Kept(Spider):
    """Renders the listing, keeping its page for the Callback."""

    def __init__(self, base: str) -> None:
        self.base = base

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{self.base}/", render=Render(keep_page=True))


async def test_a_callback_clicks_through_the_page_it_keeps(site: str) -> None:
    class Clicking(Kept):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page = response.page
            assert page is not None
            await page.click("#more")
            yield {"revealed": await page.text_content("#hidden")}
            await page.click("#next")
            await page.wait_for_url("**/next")
            yield {"next": await page.text_content("h1")}

    report = await crawl(Clicking, base=site)

    assert report.items == [{"revealed": "three more books"}, {"next": "page two"}]
    assert (report.stats["browser.pages_open"], report.stats["browser.contexts_open"]) == (0, 0)  # closed by the Run


async def test_the_page_is_closed_after_a_callback_that_raises(site: str) -> None:
    class Failing(Kept):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            assert response.page is not None
            await response.page.click("#more")
            raise ValueError("cannot parse")
            yield

    report = await crawl(Failing, base=site)

    assert report.stats["requests.failed_by_error.ValueError"] == 1
    assert (report.stats["browser.pages_open"], report.stats["browser.contexts_open"]) == (0, 0)


async def test_the_page_is_closed_when_a_force_stop_cancels_its_callback(site: str) -> None:
    entered = asyncio.Event()

    class Stalling(Kept):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            assert response.page is not None
            entered.set()
            await asyncio.sleep(60)
            yield

    crawler = Crawler.from_spider(Stalling, args={"base": site}, settings={"stats.interval": 0})
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(30):
        await entered.wait()
        crawler.stop(mode="force")
        result = await run

    assert result.reason == "forced"
    stats = crawler.stats.snapshot()
    assert (stats["browser.pages_open"], stats["browser.contexts_open"]) == (0, 0)


async def test_a_callback_whose_time_runs_out_closes_its_page_and_gives_its_slot_back(site: str) -> None:
    pages: list[Page] = []

    class Stuck(Kept):
        settings: ClassVar[Mapping[str, Any]] = {"browser.max_pages": 1, "callbacks.timeout": 0.5}

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/", render=Render(keep_page=True))
            yield Request(f"{self.base}/next", render=True, callback=self.parse_next)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            assert response.page is not None
            pages.append(response.page)
            yield {"kept": True}
            await asyncio.Event().wait()  # holds the only page slot until its time runs out

        async def parse_next(self, response: Response) -> AsyncIterator[Any]:
            yield {"next": response.css("h1::text").get()}

    report = await crawl(Stuck, base=site)

    assert sorted(report.items, key=str) == [{"kept": True}, {"next": "page two"}]
    assert report.stats["callbacks.timeouts"] == 1
    assert report.stats["requests.failed_by_error.CallbackTimeout"] == 1
    [page] = pages
    assert page.is_closed()
    assert (report.stats["browser.pages_open"], report.stats["browser.contexts_open"]) == (0, 0)


async def test_a_callback_holding_the_only_page_can_still_render(site: str) -> None:
    class Nested(Kept):
        settings: ClassVar[Mapping[str, Any]] = {"browser.max_pages": 1}

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            assert response.page is not None
            async with asyncio.timeout(20):  # the only page slot is this Callback's
                other = await self.fetch(Request(f"{self.base}/next", render=True))
            yield {"next": other.css("h1::text").get()}

    report = await crawl(Nested, base=site)

    assert report.items == [{"next": "page two"}]


async def test_an_inline_fetch_cannot_keep_its_page_but_runs_its_page_methods(site: str) -> None:
    class Inline(Spider):
        def __init__(self, base: str) -> None:
            self.base = base

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/next")

        async def mark(self, page: Page) -> None:
            await page.evaluate("() => { document.querySelector('h1').dataset.marked = 'yes' }")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            marked = await self.fetch(Request(f"{self.base}/next", render=Render(action=self.mark)))
            refused = ""
            try:
                await self.fetch(Request(f"{self.base}/next", render=Render(keep_page=True)))
            except ValueError as exc:
                refused = str(exc)
            yield {"marked": marked.css("h1::attr(data-marked)").get(), "refused": "Inline Fetch" in refused}

    report = await crawl(Inline, base=site)

    assert report.items == [{"marked": "yes", "refused": True}]


async def test_a_callback_whose_kept_page_crashed_is_retried(site: str) -> None:
    class Crashing(Kept):
        attempts = 0

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page = response.page
            assert page is not None
            self.attempts += 1
            if self.attempts == 1:
                crashed = asyncio.Event()
                page.on("crash", lambda _page: crashed.set())
                devtools = await page.context.new_cdp_session(page)
                crashing = asyncio.create_task(devtools.send("Page.crash"))  # it never answers
                await crashed.wait()
                crashing.cancel()
            yield {"text": await page.text_content("#next"), "attempt": self.attempts}

    report = await crawl(Crashing, base=site)

    assert report.items == [{"text": "next", "attempt": 2}]
    assert (report.stats["requests.retried"], report.stats["browser.page_crashes"]) == (1, 1)
    assert report.stats["requests.failed"] == 0


@pytest.mark.parametrize(
    ("victims", "lost"), [(browsers, "disconnected"), (drivers, "driver")], ids=["browser", "driver"]
)
async def test_a_callback_whose_browser_was_lost_is_retried_under_its_budget(
    site: str, victims: Callable[[], list[psutil.Process]], lost: str
) -> None:
    class Losing(Kept):
        attempts = 0

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page = response.page
            assert page is not None
            self.attempts += 1
            if self.attempts == 1:
                kill(victims())
            yield {"text": await page.text_content("#next"), "attempt": self.attempts}

    report = await crawl(Losing, base=site)

    assert report.items == [{"text": "next", "attempt": 2}]
    assert report.stats["requests.retried_by_budget.browser_lost"] == 1
    assert report.stats[f"browser.lost_by_reason.{lost}"] == 1
    assert report.stats["browser.generations"] == 2


async def test_a_callback_whose_driver_died_is_retried_though_its_process_reads_alive_at_first(
    site: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    alive = BrowserProcesses.driver_alive
    stale: list[bool] = []

    def driver_alive(processes: BrowserProcesses) -> bool:
        # A driver killed closes its connection before its process ends, so a read in between finds it running. The
        # first read after the kill judges the Callback's error; were another made first, this one would not be stale.
        return stale.pop() if stale else alive(processes)

    monkeypatch.setattr(BrowserProcesses, "driver_alive", driver_alive)

    class Losing(Kept):
        attempts = 0

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            page = response.page
            assert page is not None
            self.attempts += 1
            if self.attempts == 1:
                kill(drivers())
                stale.append(True)
            yield {"text": await page.text_content("#next"), "attempt": self.attempts}

    report = await crawl(Losing, base=site)

    assert stale == []  # the driver was read as running once
    assert report.items == [{"text": "next", "attempt": 2}]
    assert report.stats["requests.retried_by_budget.browser_lost"] == 1
    assert report.stats["browser.lost_by_reason.driver"] == 1
