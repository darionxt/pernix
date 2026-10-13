from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING

from pernix import events
from pernix.logs import RunContext
from pernix.stats import Stats, StatsReporter

if TYPE_CHECKING:
    import pytest

RUN = RunContext("run-1", "books")


@dataclasses.dataclass
class QueueGauge:
    ready: int = 2
    leased: int = 1


def _recorder(bus: events.EventBus) -> list[events.StatsSnapshot]:
    snapshots: list[events.StatsSnapshot] = []
    bus.on(events.StatsSnapshot, snapshots.append)
    return snapshots


async def test_the_final_snapshot_records_the_run_and_the_gauges(caplog: pytest.LogCaptureFixture) -> None:
    bus = events.EventBus()
    snapshots = _recorder(bus)
    stats = Stats()

    async def queue_gauge() -> QueueGauge:
        return QueueGauge()

    reporter = StatsReporter(stats, bus, RUN, interval=0, gauges={"queue": queue_gauge})
    caplog.set_level(logging.INFO, logger="pernix")

    reporter.start()
    stats.inc("downloads.responses", 4)
    await reporter.finish("finished")

    [snapshot] = snapshots
    assert snapshot.final
    assert snapshot.stats["run.id"] == "run-1"
    assert snapshot.stats["run.spider"] == "books"
    assert snapshot.stats["run.reason"] == "finished"
    assert snapshot.stats["run.started_at"] <= snapshot.stats["run.finished_at"]
    assert snapshot.stats["run.elapsed"] >= 0
    assert (snapshot.stats["queue.ready"], snapshot.stats["queue.leased"]) == (2, 1)
    assert {"loop.lag_avg", "loop.lag_max"} <= snapshot.stats.keys()
    [summary] = [r for r in caplog.records if r.name == "pernix.stats.stats_reporter"]
    assert summary.getMessage().startswith("Run finished after ")
    assert "4 responses" in summary.getMessage()
    assert summary.__dict__["final"] is True
    assert summary.__dict__["stats"] == snapshot.stats


async def test_snapshots_are_taken_every_interval() -> None:
    bus = events.EventBus()
    snapshots = _recorder(bus)
    reporter = StatsReporter(Stats(), bus, RUN, interval=0.02)

    reporter.start()
    await asyncio.sleep(0.11)
    await reporter.finish("finished")

    periodic = [snapshot for snapshot in snapshots if not snapshot.final]
    assert len(periodic) >= 2
    assert [snapshot.final for snapshot in snapshots][-1] is True
    assert periodic[0].stats["run.elapsed"] < periodic[1].stats["run.elapsed"]


async def test_a_failing_gauge_is_logged_and_skipped(caplog: pytest.LogCaptureFixture) -> None:
    def broken() -> object:
        raise RuntimeError("frontier unavailable")

    bus = events.EventBus()
    snapshots = _recorder(bus)
    reporter = StatsReporter(Stats(), bus, RUN, interval=0, gauges={"frontier": broken, "odd": lambda: 42})

    reporter.start()
    await reporter.finish("failed")

    assert snapshots[0].stats["run.reason"] == "failed"
    assert not any(key.startswith(("frontier.", "odd.")) for key in snapshots[0].stats)
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.ERROR]
    assert messages == ["Reading the frontier stats failed", "Reading the odd stats failed"]
