"""Runs whose rendered Requests have workers of their own, apart from the others."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

from pernix import Request, Response, Spider
from pernix.testing import FakeFetcher, crawl, make_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

SHOP = "https://shop.example"


class Pages(Spider):
    """Starts with its rendered pages, then the others, and reports which Fetcher each Item came from."""

    def __init__(self, rendered: int = 2, others: int = 2) -> None:
        self.rendered = rendered
        self.others = others

    async def start(self) -> AsyncIterator[Request]:
        for n in range(self.rendered):
            yield Request(f"{SHOP}/rendered/{n}", render=True, priority=1)
        for n in range(self.others):
            yield Request(f"{SHOP}/plain/{n}")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"fetcher": response.fetcher}


async def test_requests_that_do_not_render_are_fetched_while_every_page_is_busy() -> None:
    plain_done = asyncio.Event()
    plain = FakeFetcher({})

    async def answer_plain(request: Request) -> Response:
        if request.url.endswith("/1"):
            plain_done.set()
        return make_response(request.url, "<p>plain</p>", request=request)

    async def answer_rendered(request: Request) -> Response:
        async with asyncio.timeout(5):  # the Run's single worker would never get to the others
            await plain_done.wait()
        return make_response(request.url, "<p>rendered</p>", request=request)

    plain.add(f"{SHOP}/plain/0", answer_plain)
    plain.add(f"{SHOP}/plain/1", answer_plain)
    browser = FakeFetcher({})
    browser.add(f"{SHOP}/rendered/0", answer_rendered)
    browser.add(f"{SHOP}/rendered/1", answer_rendered)

    report = await crawl(Pages, fetcher=plain, browser=browser, settings={"concurrency": 1})

    assert [item["fetcher"] for item in report.items] == ["http", "http", "browser", "browser"]
    assert report.stats["requests.failed"] == 0


async def test_browser_max_pages_rendered_requests_are_processed_at_once() -> None:
    active = most = 0

    async def answer(request: Request) -> Response:
        nonlocal active, most
        active += 1
        most = max(most, active)
        try:
            await asyncio.sleep(0.02)
        finally:
            active -= 1
        return make_response(request.url, "<p>rendered</p>", request=request)

    browser = FakeFetcher({})
    for n in range(6):
        browser.add(f"{SHOP}/rendered/{n}", answer)

    report = await crawl(
        Pages, browser=browser, rendered=6, others=0, settings={"concurrency": 16, "browser.max_pages": 2}
    )

    assert len(report.items) == 6
    assert most == 2


async def test_rendered_requests_fail_without_a_browser_fetcher_and_the_run_ends() -> None:
    class Rendered(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"fetchers": {"browser": None}}

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{SHOP}/", render=True)

        async def parse(self, response: Response) -> None:
            pass

    async with asyncio.timeout(10):
        report = await crawl(Rendered, fetcher=FakeFetcher({}))

    assert report.reason == "finished"
    assert report.stats["requests.failed_by_error.LookupError"] == 1
