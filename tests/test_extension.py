from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Response, Spider, events
from pernix.crawler import Crawler
from pernix.extension import Extension

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from loopback import LoopbackServer
    from pernix.events import EventBus


class Journal(Extension):
    """Records its lifecycle and the Events it receives into a shared list."""

    entries: ClassVar[list[str]] = []

    def __init__(self, crawler: Crawler, bus: EventBus) -> None:
        self.crawler = crawler
        self.bus = bus

    async def open(self) -> None:
        self.entries.append(f"{self.label} open")

    async def close(self) -> None:
        self.entries.append(f"{self.label} close")

    @property
    def label(self) -> str:
        return type(self).__name__

    @events.on(events.SpiderOpened)
    def opened(self, event: events.SpiderOpened) -> None:
        self.entries.append(f"{self.label} opened {event.spider.name}")

    @events.on(events.SpiderClosed)
    async def closed(self, event: events.SpiderClosed) -> None:
        self.entries.append(f"{self.label} closed {event.reason}")


class FirstJournal(Journal):
    pass


class SecondJournal(Journal):
    pass


class BrokenJournal(Journal):
    async def open(self) -> None:
        raise RuntimeError("cannot open")


@pytest.fixture(autouse=True)
def _reset_journal() -> None:
    Journal.entries.clear()


def _spider(server: LoopbackServer) -> type[Spider]:
    server.route("/", "ok")

    class Single(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"ok": True}

    return Single


async def test_extensions_open_in_order_receive_events_and_close_in_reverse(server: LoopbackServer) -> None:
    spider = _spider(server)

    async with asyncio.timeout(10):
        await spider.crawl(settings={"extensions": {SecondJournal: 200, f"{__name__}.FirstJournal": 100}})

    assert Journal.entries == [
        "FirstJournal open",
        "SecondJournal open",
        "FirstJournal opened single",
        "SecondJournal opened single",
        "SecondJournal closed finished",
        "FirstJournal closed finished",
        "SecondJournal close",
        "FirstJournal close",
    ]


async def test_extensions_get_the_crawler_and_bus_injected(server: LoopbackServer) -> None:
    crawler = Crawler.from_spider(_spider(server), settings={"extensions": {FirstJournal: 1}})
    built = crawler.build_component(FirstJournal)

    assert isinstance(built, FirstJournal)
    assert built.crawler is crawler
    assert built.bus is crawler.events


async def test_an_extension_that_fails_to_open_stops_the_run_and_closes_the_others(server: LoopbackServer) -> None:
    spider = _spider(server)

    with pytest.raises(RuntimeError, match="cannot open"):
        await spider.crawl(settings={"extensions": {FirstJournal: 1, BrokenJournal: 2, SecondJournal: 3}})

    assert Journal.entries == ["FirstJournal open", "FirstJournal close"]
    assert server.received == []


async def test_a_crawler_runs_only_once(server: LoopbackServer) -> None:
    crawler = Crawler.from_spider(_spider(server))
    await crawler.crawl()

    with pytest.raises(RuntimeError, match="runs only once"):
        await crawler.crawl()
