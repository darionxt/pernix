"""Stopping whole crawls against the loopback server: Drain, Force Stop, and the Run's result."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Pipeline, Request, Response, Spider, events
from pernix.crawler import Crawler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from loopback import LoopbackServer
    from pernix.items import ItemContext


def _lines(path: Path) -> list[Any]:
    return [orjson.loads(line) for line in path.read_text().splitlines()]


@pytest.fixture
def pages(server: LoopbackServer) -> LoopbackServer:
    for n in range(1, 6):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    return server


def _spider(server: LoopbackServer, entered: asyncio.Event, gate: asyncio.Event) -> type[Spider]:
    """A Spider over five pages whose first Callback waits for `gate` once it has `entered`."""

    class Pages(Spider):
        start_urls = tuple(server.url(f"/{n}") for n in range(1, 6))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            entered.set()
            await gate.wait()
            yield {"page": response.css("h1::text").get()}
            yield Request(server.url("/later"))

    return Pages


def _crawler(spider: type[Spider], settings: Mapping[str, Any] | None = None, **options: Any) -> Crawler:
    return Crawler.from_spider(spider, settings={"concurrency": 1, "stats.interval": 0, **(settings or {})}, **options)


async def test_a_drain_finishes_the_request_in_flight_and_fetches_no_more(
    pages: LoopbackServer, tmp_path: Path
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    crawler = _crawler(_spider(pages, entered, gate), output=tmp_path / "items.jsonl")
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop()
        gate.set()
        result = await run

    assert (result.run_id, result.reason) == (crawler.run_id, "drained")
    assert closed == ["drained"]
    assert [request.path for request in pages.received] == ["/1"]
    assert _lines(tmp_path / "items.jsonl") == [{"page": "1"}]
    stats = result.stats
    assert (stats["run.reason"], stats["frontier.done"], stats["frontier.ready"]) == ("drained", 1, 5)


async def test_a_spider_may_stop_its_own_run(pages: LoopbackServer) -> None:
    class First(Spider):
        start_urls = tuple(pages.url(f"/{n}") for n in range(1, 6))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.crawler.stop()
            yield {"page": response.css("h1::text").get()}

    async with asyncio.timeout(10):
        result = await _crawler(First).crawl()

    assert result.reason == "drained"
    assert (result.stats["items.scraped"], len(pages.received)) == (1, 1)


async def test_a_force_stop_returns_the_request_in_flight_and_discards_the_queued_items(
    pages: LoopbackServer, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    processing = asyncio.Event()

    class Slow(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            processing.set()
            await asyncio.Event().wait()
            return item

    class Many(Spider):
        start_urls = (pages.url("/1"), pages.url("/2"))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(3):
                yield {"n": n}
            await asyncio.Event().wait()  # still in flight when stopped
            yield {"n": "never"}

    crawler = _crawler(Many, {"pipelines": {Slow: 100}}, output=tmp_path / "items.jsonl")
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await processing.wait()
        await asyncio.sleep(0.01)  # the other Items are queued
        crawler.stop("force")
        result = await run

    stats = result.stats
    assert result.reason == "forced"
    assert (stats["items.discarded"], stats["items.scraped"]) == (3, 0)
    assert (stats["frontier.ready"], stats["frontier.leased"], stats["frontier.done"]) == (2, 0, 0)
    assert (tmp_path / "items.jsonl").read_text() == ""  # the Feed is still closed
    assert "Stopping the Run now: cancelling the work in flight" in caplog.text


async def test_the_returned_request_keeps_its_attempt(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(_spider(pages, entered, asyncio.Event()))
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("force")
        result = await run

    assert result.stats["requests.retried"] == 0
    assert result.stats["frontier.ready"] == 5


async def test_a_drain_past_its_grace_turns_into_a_force_stop(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(_spider(pages, entered, asyncio.Event()), {"shutdown.grace": 0.05})
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("drain")
        result = await run

    assert result.reason == "forced"
    assert closed == ["forced"]


class SlowClose:
    def __init__(self, seconds: float) -> None:
        self.seconds = seconds

    async def close(self) -> None:
        await asyncio.sleep(self.seconds)


async def test_closing_after_a_force_stop_is_limited(pages: LoopbackServer) -> None:
    entered = asyncio.Event()
    crawler = _crawler(
        _spider(pages, entered, asyncio.Event()),
        {"extensions": {SlowClose(60): 100}, "shutdown.close_timeout": 0.05},
    )
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.stop("force")
        with pytest.raises(ExceptionGroup) as raised:
            await run

    [error] = raised.value.exceptions
    assert str(error) == "closing took longer than 0.05 s"


async def test_closing_after_a_finished_run_is_not_limited(pages: LoopbackServer) -> None:
    gate = asyncio.Event()
    gate.set()
    crawler = _crawler(
        _spider(pages, asyncio.Event(), gate),
        {"extensions": {SlowClose(0.1): 100}, "shutdown.close_timeout": 0.01},
    )

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "finished"


async def test_a_cancelled_run_limits_closing_too(pages: LoopbackServer, caplog: pytest.LogCaptureFixture) -> None:
    entered = asyncio.Event()
    crawler = _crawler(
        _spider(pages, entered, asyncio.Event()),
        {"extensions": {SlowClose(60): 100}, "shutdown.close_timeout": 0.05},
    )
    closed: list[str] = []
    crawler.events.on(events.SpiderClosed, lambda event: closed.append(event.reason))
    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        run.cancel()
        with pytest.raises(asyncio.CancelledError):
            await run

    assert closed == ["cancelled"]
    assert "closing took longer than 0.05 s" in caplog.text


async def test_a_stop_requested_before_the_run_takes_effect_when_it_starts(pages: LoopbackServer) -> None:
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()))

    crawler.stop()
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "drained"
    assert pages.received == []


async def test_a_stop_after_the_run_does_nothing(pages: LoopbackServer) -> None:
    gate = asyncio.Event()
    gate.set()
    crawler = _crawler(_spider(pages, asyncio.Event(), gate))
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    crawler.stop("force")

    assert result.reason == "finished"


def test_stop_modes_are_checked(pages: LoopbackServer) -> None:
    crawler = _crawler(_spider(pages, asyncio.Event(), asyncio.Event()))

    with pytest.raises(ValueError, match="the stop mode must be 'drain' or 'force'"):
        crawler.stop("pause")  # pyright: ignore[reportArgumentType]
