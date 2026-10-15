from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Pipeline, Request, Response, Spider
from pernix.errors import SpiderArgumentError, Timeout
from pernix.testing import FakeFetcher, crawl, make_response, run_callback

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.crawler import Crawler
    from pernix.items import ItemContext
    from pernix.testing import LoopbackServer

SHOP = "https://shop.example"


class Books(Spider):
    start_urls = (f"{SHOP}/",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        for href in response.css("a::attr(href)").getall():
            yield Request(href, callback=self.parse_book, kwargs={"source": "list"})

    async def parse_book(self, response: Response, source: str) -> AsyncIterator[Any]:
        yield {"title": response.css("h1::text").get(), "source": source}
        yield None


async def test_a_spider_runs_end_to_end_on_a_fake_fetcher() -> None:
    fake = FakeFetcher(
        {
            f"{SHOP}/": "<a href='/book/1'>Dune</a><a href='/book/2'>Emma</a>",
            re.compile(r"https://shop\.example/book/1"): "<h1>Dune</h1>",
            f"{SHOP}/book/2": [Timeout("slow"), "<h1>Emma</h1>"],
        }
    )

    report = await crawl(Books, fetcher=fake)

    assert sorted(item["title"] for item in report.items) == ["Dune", "Emma"]
    assert (report.reason, report.stats["items.scraped"], report.stats["requests.retried"]) == ("finished", 2, 1)
    assert report.run_id == report.stats["run.id"]
    assert fake.unmatched == []


class Upper(Pipeline):
    async def process(self, item: Any, ctx: ItemContext) -> Any:
        return {**item, "title": item["title"].upper()}


class Search(Spider):
    settings: ClassVar[Mapping[str, Any]] = {"domain.delay": 5.0, "domain.rate": 0.5}

    def __init__(self, keyword: str, pages: int = 3) -> None:
        self.keyword = keyword
        self.pages = pages

    async def start(self) -> AsyncIterator[Request]:
        for page in range(1, self.pages + 1):
            yield Request(f"{SHOP}/search", params={"q": self.keyword, "page": page})

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"title": response.text}


async def test_crawl_takes_arguments_and_settings_and_never_waits() -> None:
    fake = FakeFetcher({re.compile(r".*/search\?q=dune&page=\d"): lambda request: request.url[-1]})

    started = time.monotonic()
    report = await crawl(Search, fetcher=fake, settings={"pipelines": {Upper: 100}}, keyword="dune", pages="3")

    assert time.monotonic() - started < 2  # the Spider's delay and rate would take 10 s
    assert sorted(item["title"] for item in report.items) == ["1", "2", "3"]
    assert report.stats["items.scraped"] == 3
    with pytest.raises(SpiderArgumentError, match="keyword"):
        await crawl(Search, fetcher=fake)


async def test_without_a_fetcher_the_spider_fetches_for_real_from_a_loopback_server(server: LoopbackServer) -> None:
    server.route("/", "<a href='/book/1'>Dune</a>")
    server.route("/book/1", "<h1>Dune</h1>")

    class Local(Books):
        start_urls = (server.url("/"),)

    report = await crawl(Local)

    assert report.items == [{"title": "Dune", "source": "list"}]


class StopsAtOnce:
    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    async def open(self) -> None:
        self.crawler.stop("force")


async def test_a_run_force_stopped_while_opening_is_reported_as_forced() -> None:
    fake = FakeFetcher()

    report = await crawl(Books, fetcher=fake, settings={"extensions": {StopsAtOnce: 100}})

    assert (report.reason, report.items, fake.requests) == ("forced", [], [])


async def test_run_callback_runs_the_requested_callback_with_its_kwargs() -> None:
    books = Books()
    listing = make_response(f"{SHOP}/new/", "<a href='book/1'>Dune</a><a href='/book/2'>Emma</a>")

    requests = await run_callback(books, listing)
    book = make_response(f"{SHOP}/new/book/1", "<h1>Dune</h1>", request=requests[0])

    assert [request.url for request in requests] == [f"{SHOP}/new/book/1", f"{SHOP}/book/2"]
    assert await run_callback(books, book) == [{"title": "Dune", "source": "list"}]  # without the None


async def test_run_callback_takes_coroutine_callbacks_returning_lists() -> None:
    class Listing(Spider):
        async def parse(self, response: Response) -> list[Any]:
            return [{"n": 1}, Request("/next")]

    [item, request] = await run_callback(Listing(), make_response(f"{SHOP}/page", ""))

    assert (item, request.url) == ({"n": 1}, f"{SHOP}/next")
