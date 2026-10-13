"""Streaming the Items of whole crawls against the loopback server."""

from __future__ import annotations

import asyncio
import gc
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Pipeline, Spider, events
from pernix.crawler import Crawler
from pernix.errors import FeedError, SpiderArgumentError

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from pathlib import Path

    from loopback import LoopbackServer
    from pernix import Response
    from pernix.items import ItemContext

SETTINGS = {"stats.interval": 0}


class Tag(Pipeline):
    async def process(self, item: Any, ctx: ItemContext) -> Any:
        return {**item, "tagged": True}


def _counting(server: LoopbackServer, count: int, produced: list[int]) -> type[Spider]:
    class Counting(Spider):
        start_urls = (server.url("/"),)

        def __init__(self, start: int = 0) -> None:
            self.start_at = start

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for n in range(self.start_at, self.start_at + count):
                produced.append(n)
                yield {"n": n}

    return Counting


@pytest.fixture
def home(server: LoopbackServer) -> LoopbackServer:
    server.route("/", "<p>home</p>")
    return server


async def test_the_items_that_pass_the_pipelines_are_streamed_while_the_feeds_still_get_them(
    home: LoopbackServer, tmp_path: Path
) -> None:
    spider = _counting(home, 3, [])

    async with asyncio.timeout(10):
        stream = spider.stream(settings={**SETTINGS, "pipelines": {Tag: 100}}, output=tmp_path / "items.jsonl", start=5)
        items = [item async for item in stream]

    assert items == [{"n": 5, "tagged": True}, {"n": 6, "tagged": True}, {"n": 7, "tagged": True}]
    assert len((tmp_path / "items.jsonl").read_text().splitlines()) == 3


async def test_a_slow_consumer_pauses_the_callbacks(home: LoopbackServer) -> None:
    produced: list[int] = []
    crawler = Crawler.from_spider(_counting(home, 100, produced), settings={**SETTINGS, "items.queue_size": 2})

    async with asyncio.timeout(10), crawler.stream() as items:
        first = await anext(items)
        await asyncio.sleep(0.05)  # everything that can move has moved
        paused_at = len(produced)
        rest = [item async for item in items]

    assert first == {"n": 0}
    # One in the stream, one waiting to enter it, two queued, one waiting to be queued.
    assert paused_at == 6
    assert [item["n"] for item in rest] == list(range(1, 100))
    assert crawler.stats.get("run.reason") == "finished"


async def test_leaving_early_force_stops_the_run(home: LoopbackServer) -> None:
    produced: list[int] = []
    crawler = Crawler.from_spider(_counting(home, 100, produced), settings={**SETTINGS, "items.queue_size": 2})

    async with asyncio.timeout(10), crawler.stream() as items:
        async for item in items:
            assert item == {"n": 0}
            await asyncio.sleep(0.05)
            break

    assert crawler.stats.get("run.reason") == "forced"
    assert len(produced) == 6
    # The one waiting to enter the stream, and the two queued.
    assert crawler.stats.get("items.discarded") == 3


async def test_an_abandoned_stream_stops_once_garbage_collected(home: LoopbackServer) -> None:
    crawler = Crawler.from_spider(_counting(home, 100, []), settings=SETTINGS)
    closed: list[str] = []
    ended = asyncio.Event()
    crawler.events.on(events.SpiderClosed, lambda event: (closed.append(event.reason), ended.set()))
    items = crawler.stream()
    assert await anext(items) == {"n": 0}

    del items
    gc.collect()
    async with asyncio.timeout(10):
        await ended.wait()

    assert closed == ["forced"]


async def test_the_error_of_a_failed_run_follows_the_items_received(home: LoopbackServer) -> None:
    class FailThird(Pipeline):
        async def process(self, item: Any, ctx: ItemContext) -> Any:
            if item["n"] == 2:
                raise FeedError("the store is gone")
            return item

    items = _counting(home, 5, []).stream(settings={**SETTINGS, "pipelines": {FailThird: 100}})

    async with asyncio.timeout(10):
        received = [await anext(items), await anext(items)]
        with pytest.raises(ExceptionGroup) as raised:
            await anext(items)

    assert received == [{"n": 0}, {"n": 1}]
    assert raised.group_contains(FeedError, match="the store is gone")


def test_invalid_arguments_are_reported_when_the_stream_is_created(home: LoopbackServer) -> None:
    with pytest.raises(SpiderArgumentError, match="start: Expected `int`, got `str`"):
        _counting(home, 1, []).stream(start="first")


async def test_a_crawler_streams_only_once(home: LoopbackServer) -> None:
    crawler = Crawler.from_spider(_counting(home, 1, []), settings=SETTINGS)
    crawler.stream()

    with pytest.raises(RuntimeError, match="a Crawler runs only once"):
        crawler.stream()
