from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import inspect
import logging
import time
from typing import TYPE_CHECKING

from pernix.events import StatsSnapshot
from pernix.stats.loop_lag import LoopLag

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from pernix.events import EventBus
    from pernix.logs import RunContext
    from pernix.stats.stats import Stats

__all__ = ["Gauge", "StatsReporter"]

logger = logging.getLogger(__name__)

type Gauge = Callable[[], object]
"""Returns a dataclass, or an awaitable of one, whose fields are read into the stats."""


class StatsReporter:
    """Takes the snapshots of a Run's stats: one every `interval`, and a final one.

    `start()` records `run.id`, `run.spider`, and `run.started_at`, and starts sampling
    the event loop lag, with its watchdog, if any. Before each snapshot, `run.elapsed` and the gauges are read
    into the stats: each field of the dataclass a gauge returns is stored as
    `<prefix>.<field>`, as are `loop.lag_avg` and `loop.lag_max`. Each snapshot is
    emitted as `StatsSnapshot` and logged at INFO with the stats in the `stats`
    extra field. `finish()` stops the periodic snapshots and the sampling, records
    `run.finished_at` and `run.reason`, and takes the final snapshot, logged as the
    Run's summary.
    """

    def __init__(
        self,
        stats: Stats,
        events: EventBus,
        run: RunContext,
        *,
        interval: float,
        gauges: Mapping[str, Gauge] | None = None,
        lag: LoopLag | None = None,
    ) -> None:
        """Configure the snapshots.

        Args:
            stats: The stats of the Run.
            events: Where `StatsSnapshot` is emitted.
            run: The Run.
            interval: Seconds between two periodic snapshots; 0 for none.
            gauges: Gauges by stats prefix, read before each snapshot.
            lag: Samples the event loop lag; defaults to one sample per second.
        """
        self._stats = stats
        self._events = events
        self._run = run
        self._interval = interval
        self._lag = lag or LoopLag()
        self._gauges: dict[str, Gauge] = {**(gauges or {}), "loop": self._lag.stats}
        self._started = 0.0
        self._tasks: list[asyncio.Task[None]] = []
        self._last = (0.0, 0, 0)  # elapsed, responses, and items at the last snapshot

    def start(self) -> None:
        """Record the start of the Run and start the periodic snapshots."""
        self._started = time.monotonic()
        self._stats.set("run.id", self._run.run_id)
        self._stats.set("run.spider", self._run.spider)
        self._stats.set("run.started_at", _now())
        self._lag.start()
        if self._interval:
            self._tasks.append(asyncio.create_task(self._periodically()))

    async def finish(self, reason: str) -> dict[str, int | float | str]:
        """Stop the periodic snapshots and the sampling, and take the final snapshot.

        Args:
            reason: How the Run ended, as in `SpiderClosed`.

        Returns:
            The final snapshot.
        """
        for task in self._tasks:
            task.cancel()
        await self._lag.stop()
        if self._tasks:
            await asyncio.wait(self._tasks)
        self._stats.set("run.finished_at", _now())
        self._stats.set("run.reason", reason)
        snapshot = await self._snapshot()
        await self._events.emit(StatsSnapshot(snapshot, final=True))
        logger.info(
            "Run %s after %.1f s: %d responses, %d items, %d failed Requests",
            reason,
            snapshot["run.elapsed"],
            snapshot.get("downloads.responses", 0),
            snapshot.get("items.scraped", 0),
            snapshot.get("requests.failed", 0),
            extra={"stats": snapshot, "final": True},
        )
        return snapshot

    async def _periodically(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._report()
            except Exception:
                logger.exception("Taking a stats snapshot failed")

    async def _report(self) -> None:
        snapshot = await self._snapshot()
        await self._events.emit(StatsSnapshot(snapshot, final=False))
        elapsed = float(snapshot["run.elapsed"])
        responses = int(snapshot.get("downloads.responses", 0))
        items = int(snapshot.get("items.scraped", 0))
        last_elapsed, last_responses, last_items = self._last
        minutes = max(elapsed - last_elapsed, 1e-9) / 60
        self._last = (elapsed, responses, items)
        logger.info(
            "Fetched %d responses (%.0f/min), scraped %d items (%.0f/min)",
            responses,
            (responses - last_responses) / minutes,
            items,
            (items - last_items) / minutes,
            extra={"stats": snapshot, "final": False},
        )

    async def _snapshot(self) -> dict[str, int | float | str]:
        self._stats.set("run.elapsed", round(time.monotonic() - self._started, 3))
        for prefix, gauge in self._gauges.items():
            try:
                values = gauge()
                if inspect.isawaitable(values):
                    values = await values
                if not dataclasses.is_dataclass(values) or isinstance(values, type):
                    raise TypeError(f"a gauge must return a dataclass, got {values!r}")
                for field in dataclasses.fields(values):
                    self._stats.set(f"{prefix}.{field.name}", getattr(values, field.name))
            except Exception:
                logger.exception("Reading the %s stats failed", prefix)
        return self._stats.snapshot()


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")
