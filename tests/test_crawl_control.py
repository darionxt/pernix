"""Controlling whole crawls through control sources, against the loopback server."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Response, Spider, events
from pernix.crawler import Crawler

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from pernix.control import ControlState
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


def _crawler(spider: type[Spider], settings: Mapping[str, Any]) -> tuple[Crawler, list[str]]:
    """A Crawler reading its control sources often, and the lifecycle Events of its Run as they are emitted."""
    crawler = Crawler.from_spider(
        spider, settings={"concurrency": 1, "stats.interval": 0, "control.interval": 0.01, **settings}
    )
    emitted: list[str] = []
    for event_type in _LIFECYCLE:
        crawler.events.on(event_type, lambda event: emitted.append(type(event).__name__))
    return crawler, emitted


def _write(path: Path, state: str) -> None:
    """Write a control file as a Platform should: atomically."""
    temporary = path.with_name(f"{path.name}.tmp")
    temporary.write_text(state)
    os.replace(temporary, path)


def _open() -> asyncio.Event:
    gate = asyncio.Event()
    gate.set()
    return gate


def _paths(server: LoopbackServer) -> list[str]:
    return [request.path for request in server.received]


async def test_writing_paused_to_the_file_pauses_the_run_and_running_resumes_it(
    pages: LoopbackServer, tmp_path: Path
) -> None:
    entered, gate, paused = asyncio.Event(), asyncio.Event(), asyncio.Event()
    state = tmp_path / "state"
    crawler, emitted = _crawler(_spider(pages, entered, gate), {"control.file": str(state), "stats.interval": 0.01})
    crawler.events.on(events.SpiderPaused, lambda _event: paused.set())
    fetched_while_paused: list[list[str]] = []

    def snapshot(event: events.StatsSnapshot) -> None:
        stats = event.stats
        quiet = stats.get("control.state") == "paused" and crawler.paused and not stats["frontier.leased"]
        if quiet and not fetched_while_paused:  # the Request in flight finished, and nothing more was claimed
            fetched_while_paused.append(_paths(pages))
            _write(state, "running")

    crawler.events.on(events.StatsSnapshot, snapshot)

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        _write(state, "paused")
        await paused.wait()
        gate.set()
        result = await run

    assert fetched_while_paused == [["/1"]]
    assert result.reason == "finished"
    assert sorted(_paths(pages)) == ["/1", "/2", "/3"]
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]
    assert result.stats["control.state"] == "running"


@pytest.mark.parametrize(("state", "reason", "scraped"), [("drain", "drained", 1), ("force", "forced", 0)])
async def test_writing_drain_or_force_stops_the_run(
    pages: LoopbackServer, tmp_path: Path, state: ControlState, reason: str, scraped: int
) -> None:
    entered, gate, applied = asyncio.Event(), asyncio.Event(), asyncio.Event()
    path = tmp_path / "state"
    crawler, emitted = _crawler(_spider(pages, entered, gate), {"control.file": str(path), "stats.interval": 0.01})
    # The state read is applied before the stats show it, so a snapshot showing it follows the Drain.
    crawler.events.on(
        events.StatsSnapshot, lambda event: applied.set() if event.stats.get("control.state") == state else None
    )

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        _write(path, state)
        if state == "drain":  # the Request in flight finishes once released; a Force Stop cancels it
            await applied.wait()
            gate.set()
        result = await run

    assert (result.reason, result.stats["items.scraped"]) == (reason, scraped)
    assert _paths(pages) == ["/1"]
    assert emitted == ["SpiderOpened", "SpiderClosed"]


async def test_a_run_whose_file_says_paused_starts_paused(pages: LoopbackServer, tmp_path: Path) -> None:
    path = tmp_path / "state"
    _write(path, "paused\n")
    crawler, emitted = _crawler(_spider(pages, asyncio.Event(), _open()), {"control.file": str(path)})
    fetched_while_paused: list[list[str]] = []

    def paused(_event: events.SpiderPaused) -> None:
        fetched_while_paused.append(_paths(pages))
        _write(path, "running")

    crawler.events.on(events.SpiderPaused, paused)

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert fetched_while_paused == [[]]
    assert result.reason == "finished"
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]


@pytest.mark.parametrize(("state", "reason", "emitted"), [("drain", "drained", 2), ("force", "forced", 0)])
async def test_a_stop_left_in_the_file_stops_the_next_run_at_once(
    pages: LoopbackServer, tmp_path: Path, state: ControlState, reason: str, emitted: int
) -> None:
    path = tmp_path / "state"
    _write(path, state)
    crawler, events_emitted = _crawler(_spider(pages, asyncio.Event(), _open()), {"control.file": str(path)})

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == reason
    assert pages.received == []
    assert len(events_emitted) == emitted  # a Force Stop while opening ends the Run before SpiderOpened


async def test_a_pause_made_by_code_is_kept_while_the_file_still_says_running(
    pages: LoopbackServer, tmp_path: Path
) -> None:
    class PausesItself(Spider):
        start_urls = (pages.url("/1"),)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.crawler.pause()
            yield {"page": response.css("h1::text").get()}

    crawler, emitted = _crawler(PausesItself, {"control.file": str(tmp_path / "state"), "stats.interval": 0.01})
    held: list[bool] = []

    def snapshot(event: events.StatsSnapshot) -> None:
        if event.final or not crawler.paused:
            return
        held.append(event.stats["control.state"] == "running")
        if len(held) == 3:  # the file was read many times meanwhile
            crawler.resume()

    crawler.events.on(events.StatsSnapshot, snapshot)

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert held == [True, True, True]
    assert result.reason == "finished"
    assert emitted == ["SpiderOpened", "SpiderPaused", "SpiderResumed", "SpiderIdle", "SpiderClosed"]


class Failing:
    reads = 0
    read_thrice = asyncio.Event()

    async def state(self) -> ControlState:
        Failing.reads += 1
        if Failing.reads == 3:
            Failing.read_thrice.set()
        raise ConnectionError("the store is down")


async def test_a_source_that_raises_does_not_end_the_run(
    pages: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="pernix")
    entered, gate = asyncio.Event(), asyncio.Event()
    crawler, _ = _crawler(_spider(pages, entered, gate), {"control.sources": {Failing: 10}})
    Failing.reads, Failing.read_thrice = 0, asyncio.Event()

    run = asyncio.create_task(crawler.crawl())
    async with asyncio.timeout(10):
        await entered.wait()
        await Failing.read_thrice.wait()  # read again and again while the Request is in flight
        gate.set()
        result = await run

    assert result.reason == "finished"
    assert result.stats["control.failures"] >= 3
    warnings = [record.getMessage() for record in caplog.records if record.name == "pernix.control.run_control"]
    assert warnings == [
        "Reading the control source Failing failed; keeping the state it told before, running: "
        "ConnectionError: the store is down"
    ]


async def test_without_a_source_nothing_is_read(pages: LoopbackServer) -> None:
    crawler = Crawler.from_spider(_spider(pages, asyncio.Event(), _open()), settings={"stats.interval": 0})

    async with asyncio.timeout(10):
        result = await crawler.crawl()

    assert result.reason == "finished"
    assert not any(key.startswith("control.") for key in result.stats)
