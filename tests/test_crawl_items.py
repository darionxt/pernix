"""Items, Pipelines, and Feeds in whole crawls against the loopback server."""

from __future__ import annotations

import asyncio
import csv
import dataclasses
import io
from typing import TYPE_CHECKING, Any, ClassVar

import attrs
import msgspec
import orjson
import pydantic
import pytest

from pernix import DropItem, ItemContext, Pipeline, Request, Response, Spider, events
from pernix.crawler import Crawler
from pernix.errors import ComponentError, FeedError, SettingsError
from pernix.settings import Section

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from loopback import LoopbackServer
    from pernix.items import FailedItem
    from pernix.stats import Stats


@dataclasses.dataclass
class Book:
    title: str
    price: float


class Author(msgspec.Struct):
    name: str


class Review(pydantic.BaseModel):
    stars: int


@attrs.define
class Shop:
    city: str


def _text(path: Path) -> str:
    return path.read_text()


def _names(directory: Path, pattern: str = "*") -> list[str]:
    return sorted(path.name for path in directory.glob(pattern))


async def _crawl(spider: type[Spider], settings: Mapping[str, Any] | None = None, **options: Any) -> Crawler:
    crawler = Crawler.from_spider(spider, settings=settings, **options)
    async with asyncio.timeout(10):
        await crawler.crawl()
    return crawler


@pytest.fixture
def catalogue(server: LoopbackServer) -> LoopbackServer:
    server.route("/", "<a href='/b/1'>1</a><a href='/b/2'>2</a>")
    server.route("/b/1", "<h1>Dune</h1>")
    server.route("/b/2", "<h1>Emma</h1>")
    return server


def _books(server: LoopbackServer) -> type[Spider]:
    class Books(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for href in response.css("a::attr(href)").getall():
                yield Request(href, callback=self.parse_book)

        async def parse_book(self, response: Response) -> AsyncIterator[Any]:
            yield Book(response.css("h1::text").get(""), 9.5)

    return Books


@pytest.mark.parametrize("name", ["books.jsonl", "books.csv", "books.json"])
async def test_output_writes_a_feed_in_the_format_of_its_extension(
    catalogue: LoopbackServer, tmp_path: Path, name: str
) -> None:
    await _crawl(_books(catalogue), output=tmp_path / name)

    text = _text(tmp_path / name)
    match name:
        case "books.jsonl":
            rows = [orjson.loads(line) for line in text.splitlines()]
        case "books.csv":
            rows = [{**row, "price": float(row["price"])} for row in csv.DictReader(io.StringIO(text))]
        case _:
            rows = orjson.loads(text)
    assert sorted(rows, key=lambda row: row["title"]) == [
        {"title": "Dune", "price": 9.5},
        {"title": "Emma", "price": 9.5},
    ]


async def test_every_item_type_reaches_the_feeds_that_accept_it(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "ok")

    class Mixed(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield Book("Dune", 9.5)
            yield Author("Herbert")
            yield Review(stars=5)
            yield Shop("Paris")
            yield {"kind": "note", "text": "plain"}

    feeds = {
        str(tmp_path / "{name}-all-{run_id}.jsonl"): {},
        str(tmp_path / "books-{time}.csv"): {"accepts": [Book]},
        str(tmp_path / "notes.json"): {"accepts": lambda item: isinstance(item, dict) and item["kind"] == "note"},
    }

    crawler = await _crawl(Mixed, {"feeds": feeds, "run_id": "r1"})

    every = _text(tmp_path / "mixed-all-r1.jsonl").splitlines()
    assert [orjson.loads(line) for line in every] == [
        {"title": "Dune", "price": 9.5},
        {"name": "Herbert"},
        {"stars": 5},
        {"city": "Paris"},
        {"kind": "note", "text": "plain"},
    ]
    [books] = _names(tmp_path, "books-*.csv")
    assert books.endswith("Z.csv")
    assert _text(tmp_path / books).splitlines() == ["title,price", "Dune,9.5"]
    assert orjson.loads(_text(tmp_path / "notes.json")) == [{"kind": "note", "text": "plain"}]
    assert crawler.stats.get("items.scraped") == 5


class PipelineSettings(Section):
    __prefix__ = "test_pipeline"
    currency: str = "EUR"


class Price(Pipeline):
    calls: ClassVar[list[str]] = []

    def __init__(self, settings: PipelineSettings, stats: Stats) -> None:
        self.currency = settings.currency
        self.stats = stats

    async def open(self) -> None:
        self.calls.append("open price")

    async def process(self, item: Any, ctx: ItemContext) -> Any:
        if item.price > 9:
            raise DropItem("too expensive")
        self.stats.inc("test.priced")
        return {"title": item.title, "price": f"{item.price} {self.currency}", "url": ctx.response and ctx.response.url}

    async def close(self) -> None:
        self.calls.append("close price")


class Fail(Pipeline):
    async def process(self, item: Any, ctx: ItemContext) -> Any:
        if item["title"] == "Emma":
            raise ValueError("no Emma")
        return item


async def test_pipelines_run_in_order_and_report_drops_and_errors(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "ok")
    Price.calls.clear()
    reported: list[tuple[str, Any]] = []

    class Watch:
        @events.on(events.ItemDropped)
        def dropped(self, event: events.ItemDropped) -> None:
            reported.append(("dropped", event.reason))

        @events.on(events.ItemError)
        def failed(self, event: events.ItemError) -> None:
            reported.append(("error", type(event.error).__name__))

    class Shelf(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for title, price in (("Dune", 5.0), ("Emma", 6.0), ("Kim", 12.0)):
                yield Book(title, price)

    settings = {
        "pipelines": {Fail: 200, Price: 100},
        "test_pipeline.currency": "USD",
        "extensions": {Watch: 1},
        "items.failed_output": str(tmp_path / "failed-{name}.jsonl"),
    }
    crawler = await _crawl(Shelf, settings, output=tmp_path / "books.jsonl")

    assert [orjson.loads(line) for line in _text(tmp_path / "books.jsonl").splitlines()] == [
        {"title": "Dune", "price": "5.0 USD", "url": server.url("/")}
    ]
    assert sorted(reported) == [("dropped", "too expensive"), ("error", "ValueError")]
    [failed] = [orjson.loads(line) for line in _text(tmp_path / "failed-shelf.jsonl").splitlines()]
    assert failed == {
        "pipeline": "Fail",
        "error": "ValueError: no Emma",
        "url": server.url("/"),
        "item": {"title": "Emma", "price": "6.0 USD", "url": server.url("/")},
    }
    assert Price.calls == ["open price", "close price"]
    stats = crawler.stats.snapshot()
    assert (stats["items.scraped"], stats["items.dropped"], stats["items.errors"], stats["test.priced"]) == (1, 1, 1, 2)


async def test_hanging_pipelines_time_out_and_failed_items_reach_the_failure_pipelines(
    server: LoopbackServer, tmp_path: Path
) -> None:
    server.route("/", "ok")
    dead_letters: list[tuple[Any, str, str]] = []

    class Hangs(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            if item["n"] == 2:
                await asyncio.Event().wait()
            return item

    class DeadLetters(Pipeline):
        async def process(self, item: FailedItem, ctx: ItemContext) -> FailedItem:
            dead_letters.append((item.item, item.pipeline, type(item.error).__name__))
            return item

    class Three(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in (1, 2, 3):
                yield {"n": n}

    settings = {
        "pipelines": {Hangs: 1},
        "failed_pipelines": {DeadLetters: 50},  # added to the built-in FailedItemsFile
        "items.timeout": 0.05,
        "items.failed_output": str(tmp_path / "failed.jsonl"),
    }
    crawler = await _crawl(Three, settings, output=tmp_path / "items.jsonl")

    assert _text(tmp_path / "items.jsonl").splitlines() == ['{"n":1}', '{"n":3}']
    assert [(item, pipeline.rsplit(".", 1)[-1], error) for item, pipeline, error in dead_letters] == [
        ({"n": 2}, "Hangs", "TimeoutError")
    ]
    [failed] = [orjson.loads(line) for line in _text(tmp_path / "failed.jsonl").splitlines()]
    assert failed["error"].endswith("Hangs took longer than 0.05 s")
    assert crawler.stats.get("items.errors") == 1


async def test_a_slow_pipeline_suspends_the_callback(server: LoopbackServer) -> None:
    server.route("/", "ok")
    release, fourth = asyncio.Event(), asyncio.Event()
    produced: list[int] = []

    class Gate(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            await release.wait()
            return item

    class Prolific(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(10):
                produced.append(n)
                if n == 3:
                    fourth.set()
                yield {"n": n}

    crawl = asyncio.create_task(_crawl(Prolific, {"pipelines": {Gate: 1}, "items.queue_size": 2}))
    async with asyncio.timeout(5):
        await fourth.wait()
    await asyncio.sleep(0.05)

    assert produced == [0, 1, 2, 3]  # one Item in the Pipeline, two queued, the fourth waits
    release.set()
    crawler = await crawl
    assert crawler.stats.get("items.scraped") == 10


async def test_batches_are_flushed_before_the_spider_closes(server: LoopbackServer) -> None:
    server.route("/", "ok")
    order: list[str] = []

    class Rows(Pipeline):
        batch_size = 4
        batch_timeout = 60.0

        async def process_batch(self, items: list[Any], contexts: list[ItemContext]) -> None:
            order.append(f"batch {[item['n'] for item in items]}")

    class Watch:
        @events.on(events.SpiderClosed)
        def closed(self, event: events.SpiderClosed) -> None:
            order.append(f"closed {event.reason}")

    class Few(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(6):
                yield {"n": n}

    crawler = await _crawl(Few, {"pipelines": {Rows: 1}, "extensions": {Watch: 1}})

    assert order == ["batch [0, 1, 2, 3]", "batch [4, 5]", "closed finished"]
    assert crawler.stats.get("items.scraped") == 6


async def test_feeds_are_finished_when_the_run_fails(server: LoopbackServer, tmp_path: Path) -> None:
    server.route("/", "ok")

    class Broken(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"n": 1}
            yield {"n": 2, "raw": b"\x00"}  # bytes have no JSON form

    feeds = {
        str(tmp_path / "good.json"): {"accepts": lambda item: "raw" not in item},
        str(tmp_path / "bad.jsonl"): {"accepts": lambda item: "raw" in item},
    }
    with pytest.RaisesGroup(pytest.RaisesExc(FeedError, match=r"bad\.jsonl: Type is not JSON serializable: bytes")):
        await _crawl(Broken, {"feeds": feeds})

    assert orjson.loads(_text(tmp_path / "good.json")) == [{"n": 1}]
    assert _text(tmp_path / "bad.jsonl") == ""
    assert _names(tmp_path) == ["bad.jsonl", "good.json"]


async def test_invalid_pipelines_and_outputs_fail_before_the_run(tmp_path: Path) -> None:
    class Idle(Spider):
        pass

    class NotAPipeline:
        pass

    with pytest.raises(ComponentError, match="it must have process"):
        await Idle.crawl(settings={"pipelines": {NotAPipeline: 1}})
    with pytest.raises(SettingsError, match="cannot infer the feed format"):
        await Idle.crawl(output=tmp_path / "items.txt")
    assert _names(tmp_path) == []
