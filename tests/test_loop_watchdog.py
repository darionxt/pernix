"""The loop watchdog, alone and in whole crawls."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix import Extension, Response, Spider, events
from pernix.crawler import Crawler
from pernix.errors import SettingsError
from pernix.logs import RunContext, current_run, run_context
from pernix.stats import LoopLag, LoopWatchdog, Stats
from pernix.testing import FakeFetcher, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pernix.testing import CrawlReport

RUN = RunContext("run-1", "books")
SHOP = "https://shop.example"


class WatchdogRecords(logging.Handler):
    """Keeps the records of the loop watchdog with the Run they were logged for, and tells when it logs."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.records: list[logging.LogRecord] = []
        self.runs: list[RunContext | None] = []
        self.warned = threading.Event()
        self.ran_again = threading.Event()

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)
        self.runs.append(current_run())
        (self.warned if record.levelno >= logging.WARNING else self.ran_again).set()


@pytest.fixture
def watchdog_records(caplog: pytest.LogCaptureFixture) -> Iterator[WatchdogRecords]:
    handler = WatchdogRecords()
    logger = logging.getLogger("pernix.stats.loop_watchdog")
    caplog.set_level(logging.INFO, logger=logger.name)
    logger.addHandler(handler)
    yield handler
    logger.removeHandler(handler)


def block_the_loop_until(event: threading.Event) -> None:
    assert event.wait(30), "the watchdog never warned"
    event.clear()


def watchdog_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name.startswith("pernix-watchdog")]


async def test_each_block_is_warned_about_once_with_its_stack_and_counted_once_the_loop_beats_again(
    watchdog_records: WatchdogRecords,
) -> None:
    stats = Stats()
    # Sampling beats four times per threshold at least, so the hour asked for here is not waited for.
    lag = LoopLag(interval=3600, watchdog=LoopWatchdog(0.05, stats=stats))
    with run_context(RUN):
        lag.start()

    for _ in range(2):
        block_the_loop_until(watchdog_records.warned)  # blocks the event loop
        assert await asyncio.to_thread(watchdog_records.ran_again.wait, 30), "the loop never beat again"
        watchdog_records.ran_again.clear()
    assert len(watchdog_threads()) == 1  # still watching
    await lag.stop()

    levels = [record.levelno for record in watchdog_records.records]
    assert levels == [logging.WARNING, logging.INFO, logging.WARNING, logging.INFO]
    warning, ran_again = watchdog_records.records[:2]
    assert warning.getMessage().startswith("The event loop has been blocked for ")
    assert warning.getMessage().endswith(" s; nothing in the Run runs until the code below returns or awaits")
    assert warning.stack_info is not None
    assert warning.stack_info.startswith("Stack (most recent call last):\n")
    assert "in block_the_loop_until" in warning.stack_info
    assert "in test_each_block_is_warned_about_once" in warning.stack_info
    assert ran_again.getMessage().startswith("The event loop ran again after being blocked for ")
    assert watchdog_records.runs == [RUN] * 4
    snapshot = stats.snapshot()
    assert snapshot["loop.blocks"] == 2
    assert float(snapshot["loop.block_seconds"]) >= 2 * 0.05
    assert watchdog_threads() == []


def test_stopping_ends_the_block_in_progress_and_the_thread(watchdog_records: WatchdogRecords) -> None:
    stats = Stats()
    watchdog = LoopWatchdog(0.01, stats=stats)

    with run_context(RUN):
        watchdog.start(0)  # the calling thread stands for the loop, which never beats
        assert [thread.name for thread in watchdog_threads()] == ["pernix-watchdog-run-1"]
        block_the_loop_until(watchdog_records.warned)
        watchdog.stop()

    assert [record.levelno for record in watchdog_records.records] == [logging.WARNING, logging.INFO]
    assert "in block_the_loop_until" in str(watchdog_records.records[0].stack_info)
    assert watchdog_records.runs == [RUN, RUN]
    snapshot = stats.snapshot()
    assert snapshot["loop.blocks"] == 1
    assert float(snapshot["loop.block_seconds"]) >= 0.01
    assert watchdog_threads() == []


async def test_the_loop_is_watched_from_the_start_of_the_sampling_before_its_task_runs(
    watchdog_records: WatchdogRecords,
) -> None:
    stats = Stats()
    lag = LoopLag(watchdog=LoopWatchdog(0.05, stats=stats))

    lag.start()
    block_the_loop_until(watchdog_records.warned)  # before the sampling task ever ran
    await lag.stop()

    assert [record.levelno for record in watchdog_records.records] == [logging.WARNING, logging.INFO]
    assert stats.get("loop.blocks") == 1
    assert watchdog_threads() == []


def test_the_blocks_start_at_zero_and_the_threshold_must_be_positive() -> None:
    stats = Stats()
    LoopWatchdog(30, stats=stats)

    assert stats.snapshot() == {"loop.block_seconds": 0.0, "loop.blocks": 0}
    with pytest.raises(ValueError, match="threshold must be positive"):
        LoopWatchdog(0, stats=stats)


def test_a_watchdog_watches_only_once(watchdog_records: WatchdogRecords) -> None:
    watchdog = LoopWatchdog(30, stats=Stats())
    watchdog.start(1)
    try:
        with pytest.raises(RuntimeError, match="watches only once"):
            watchdog.start(1)
    finally:
        watchdog.stop()

    assert watchdog_records.records == []
    assert watchdog_threads() == []


async def test_a_callback_blocking_the_event_loop_is_warned_about_with_its_stack(
    watchdog_records: WatchdogRecords, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    class Blocking(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            block_the_loop_until(watchdog_records.warned)  # as time.sleep() would, until the watchdog warns
            yield {"url": response.url}

    report = await crawl(
        Blocking,
        fetcher=FakeFetcher({f"{SHOP}/": "ok"}),
        settings={"loop.block_threshold": 0.2, "log.output": "stderr", "log.format": "json"},
    )

    lines = [orjson.loads(line) for line in stream.getvalue().splitlines()]
    [warning, ran_again] = [line for line in lines if line["logger"] == "pernix.stats.loop_watchdog"]
    assert (warning["level"], warning["run_id"]) == ("WARNING", report.run_id)
    assert warning["message"].startswith("The event loop has been blocked for ")
    assert "in parse" in warning["stack"]
    assert "in block_the_loop_until" in warning["stack"]
    assert (ran_again["level"], ran_again["run_id"]) == ("INFO", report.run_id)
    assert ran_again["message"].startswith("The event loop ran again after being blocked for ")
    assert (report.stats["loop.blocks"], report.stats["log.warning"]) == (1, 1)
    assert float(report.stats["loop.block_seconds"]) >= 0.2
    assert report.items == [{"url": f"{SHOP}/"}]


async def test_a_spider_opened_subscriber_blocking_the_event_loop_is_warned_about(
    watchdog_records: WatchdogRecords,
) -> None:
    class Opening(Extension):
        @events.on(events.SpiderOpened)
        def opened(self, _event: events.SpiderOpened) -> None:
            block_the_loop_until(watchdog_records.warned)

    class Quick(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"url": response.url}

    settings = {"loop.block_threshold": 0.2, "extensions": {Opening: 1}}
    report = await crawl(Quick, fetcher=FakeFetcher({f"{SHOP}/": "ok"}), settings=settings)

    [warning] = [record for record in watchdog_records.records if record.levelno >= logging.WARNING]
    assert "in opened" in str(warning.stack_info)
    assert report.stats["loop.blocks"] == 1


async def test_a_run_whose_loop_is_never_blocked_long_warns_of_nothing_and_leaves_no_thread(
    watchdog_records: WatchdogRecords,
) -> None:
    watching: list[list[str]] = []

    class Quick(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            watching.append([thread.name for thread in watchdog_threads()])
            yield {"url": response.url}

    report = await crawl(Quick, fetcher=FakeFetcher({f"{SHOP}/": "ok"}))

    assert watching == [[f"pernix-watchdog-{report.run_id[:8]}"]]
    assert watchdog_records.records == []
    assert (report.stats["loop.blocks"], report.stats["loop.block_seconds"]) == (0, 0.0)
    assert watchdog_threads() == []


async def test_two_runs_each_watch_their_own_loop(watchdog_records: WatchdogRecords) -> None:
    reports: dict[str, CrawlReport | BaseException] = {}
    watching: list[str] = []

    class Blocking(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            watching.extend(thread.name for thread in watchdog_threads())
            block_the_loop_until(watchdog_records.warned)
            yield {"url": response.url}

    def crawl_blocking() -> None:
        settings = {"loop.block_threshold": 0.2}
        try:
            reports["blocking"] = asyncio.run(
                crawl(Blocking, fetcher=FakeFetcher({f"{SHOP}/": "ok"}), settings=settings)
            )
        except BaseException as exc:
            reports["blocking"] = exc

    class Waiting(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            other_loop = threading.Thread(target=crawl_blocking)
            other_loop.start()
            await asyncio.to_thread(other_loop.join)  # this loop runs meanwhile
            yield {"url": response.url}

    waiting = await crawl(Waiting, fetcher=FakeFetcher({f"{SHOP}/": "ok"}))

    blocking = reports["blocking"]
    assert not isinstance(blocking, BaseException)
    assert sorted(watching) == sorted(f"pernix-watchdog-{report.run_id[:8]}" for report in (blocking, waiting))
    [warning] = [record for record in watchdog_records.records if record.levelno >= logging.WARNING]
    assert "in block_the_loop_until" in str(warning.stack_info)
    assert [run.run_id for run in watchdog_records.runs if run is not None] == [blocking.run_id] * 2
    assert (blocking.stats["loop.blocks"], blocking.stats["log.warning"]) == (1, 1)
    assert (waiting.stats["loop.blocks"], waiting.stats["log.warning"]) == (0, 0)
    assert watchdog_threads() == []


async def test_a_threshold_of_zero_turns_the_watchdog_off() -> None:
    watching: list[list[str]] = []

    class Quick(Spider):
        start_urls = (f"{SHOP}/",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            watching.append([thread.name for thread in watchdog_threads()])
            yield {"url": response.url}

    report = await crawl(Quick, fetcher=FakeFetcher({f"{SHOP}/": "ok"}), settings={"loop.block_threshold": 0})

    assert watching == [[]]
    assert not {"loop.blocks", "loop.block_seconds"} & report.stats.keys()
    assert {"loop.lag_avg", "loop.lag_max"} <= report.stats.keys()


def test_the_threshold_may_not_be_negative() -> None:
    class Quick(Spider):
        start_urls = (f"{SHOP}/",)

    with pytest.raises(SettingsError, match=r"loop\.block_threshold"):
        Crawler.from_spider(Quick, settings={"loop.block_threshold": -1})
