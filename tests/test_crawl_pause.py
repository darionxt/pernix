"""Pausing and resuming whole crawls against the loopback server."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Response, Spider, events
from pernix.crawler import Crawler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.shutdown import StopMode
    from pernix.testing import LoopbackServer

_LIFECYCLE = (
    events.SpiderOpened,
    events.SpiderPaused,
    events.SpiderResumed,
    events.SpiderIdle,
    events.SpiderClosed,
)


@pytest.fixture
def pages(server: LoopbackServer) -> LoopbackServer:
    for n in range(1, 4):
        server.route(f"/{n}", f"<h1>{n}</h1>")
    return server


def _spider(server: LoopbackServer, entered: asyncio.Event, gate: asyncio.Event) -> type[Spider]:
    """A Spider over three pages whose first Callback waits for `gate` once it has `entered`."""

    class Pages(Spider):
        start_urls = tuple(server.url(f"/{n}") for n in range(1, 4))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            if response.url.endswith("/1"):
                entered.set()
                await gate.wait()
            yield {"page": response.css("h1::text").get()}

    return Pages


def _crawler(spider: type[Spider], settings: Mapping[str, Any] | None = None) -> tuple[Crawler, list[str]]:
    """A Crawler, and the lifecycle Events of its Run as they are emitted."""
    crawler = Crawler.from_spider(spider, settings={"concurrency": 1, "stats.interval": 0, **(settings or {})})
    emitted: list[str] = []
    for event_type in _LIFECYCLE:
        crawler.events.on(event_type, lambda event: emitted.append(type(event).__name__))
    return crawler, emitted


def _resume_once_quiet(crawler: Crawler, quiet: int, seen: list[Mapping[str, Any]]) -> None:
    """Resume the Run on the `quiet`-th snapshot taken while it is paused and holds no Lease, keeping those snapshots."""

    def snapshot(event: events.StatsSnapshot) -> None:
        if event.final or not crawler.paused or event.stats["frontier.leased"]:
            return
        seen.append(event.stats)
        if len(seen) == quiet:
            crawler.resume()

    crawler.events.on(events.StatsSnapshot, snapshot)


async def test_a_paused_run_finishes_the_request_in_flight_and_fetches_again_once_resumed(
    pages: LoopbackServer,
) -> None:
    entered, gate = asyncio.Event(), asyncio.Event()
    crawler, emitted = _crawler(_spider(pages, entered, gate), {"stats.interval": 0.01})
    quiet: list[Mapping[str, Any]] = []
    _resume_once_quiet(crawler, 1, quiet)
    fetched_while_paused: list[list[str]] = []
    crawler.events.on(events.SpiderResumed, lambda _event: fetched_while_paused.append(_paths(pages)))

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.pause()
        gate.set()
        result = await run

    assert fetched_while_paused == [["/1"]]
    [stats] = quiet  # snapshots go on while paused
    assert (stats["run.pauses"], stats["frontier.ready"], stats["items.scraped"]) == (1, 2, 1)
    assert result.reason == "finished"
    assert sorted(_paths(pages)) == ["/1", "/2", "/3"]
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]
    assert result.stats["run.pauses"] == 1
    assert result.stats["run.paused_seconds"] >= stats["run.paused_seconds"]


async def test_a_paused_run_with_nothing_left_does_not_end_until_resumed(pages: LoopbackServer) -> None:
    class PausesItself(Spider):
        start_urls = (pages.url("/1"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.crawler.pause()
            yield {"page": response.css("h1::text").get()}

    crawler, emitted = _crawler(PausesItself, {"stats.interval": 0.01})
    quiet: list[Mapping[str, Any]] = []
    _resume_once_quiet(crawler, 2, quiet)
    emitted_before_resuming: list[list[str]] = []
    crawler.events.on(events.SpiderResumed, lambda _event: emitted_before_resuming.append(list(emitted)))

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert emitted_before_resuming == [["SpiderOpened", "SpiderPaused", "SpiderResumed"]]  # no SpiderIdle yet
    assert [stats["frontier.ready"] for stats in quiet] == [0, 0]
    assert result.reason == "finished"
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]


async def test_a_run_paused_before_it_starts_starts_paused(pages: LoopbackServer) -> None:
    crawler, emitted = _crawler(_spider(pages, asyncio.Event(), _open()))
    fetched_while_paused: list[list[str]] = []

    def paused(_event: events.SpiderPaused) -> None:
        fetched_while_paused.append(_paths(pages))
        crawler.resume()

    crawler.events.on(events.SpiderPaused, paused)

    crawler.pause()
    assert crawler.paused
    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert fetched_while_paused == [[]]
    assert result.reason == "finished"
    assert sorted(_paths(pages)) == ["/1", "/2", "/3"]
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]
    assert not crawler.paused


@pytest.mark.parametrize(
    ("mode", "reason", "settled"),
    [("drain", "drained", (1, 1, 2)), ("force", "forced", (0, 0, 3))],  # Items scraped, Requests done and ready
)
async def test_a_stop_ends_a_paused_run(
    pages: LoopbackServer, mode: StopMode, reason: str, settled: tuple[int, int, int]
) -> None:
    entered, gate, paused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    crawler, emitted = _crawler(_spider(pages, entered, gate))
    crawler.events.on(events.SpiderPaused, lambda _event: paused.set())

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        crawler.pause()
        await paused.wait()
        crawler.stop(mode)
        if mode == "drain":  # the Request in flight finishes; a Force Stop cancels it
            gate.set()
        result = await run

    stats = result.stats
    assert result.reason == reason
    assert (stats["items.scraped"], stats["frontier.done"], stats["frontier.ready"]) == settled
    assert (stats["frontier.leased"], _paths(pages)) == (0, ["/1"])
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderClosed"]


async def test_pausing_once_stopping_or_after_the_run_does_nothing(pages: LoopbackServer) -> None:
    crawler, emitted = _crawler(_spider(pages, asyncio.Event(), _open()))

    crawler.stop()
    crawler.pause()
    assert not crawler.paused
    async with asyncio.timeout(10):
        result = await crawler.crawl()
    crawler.pause()

    assert result.reason == "drained"
    assert not crawler.paused
    assert emitted == ["SpiderOpened", "SpiderClosed"]


async def test_a_run_never_paused_counts_no_pause(pages: LoopbackServer) -> None:
    crawler, emitted = _crawler(_spider(pages, asyncio.Event(), _open()))
    crawler.resume()

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert (result.stats["run.pauses"], result.stats["run.paused_seconds"]) == (0, 0.0)
    assert emitted == ["SpiderOpened", "SpiderIdle", "SpiderClosed"]


def _open() -> asyncio.Event:
    gate = asyncio.Event()
    gate.set()
    return gate


def _paths(server: LoopbackServer) -> list[str]:
    return [request.path for request in server.received]
