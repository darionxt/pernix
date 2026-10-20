from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pernix.stats.loop_watchdog import LoopWatchdog

__all__ = ["LoopLag", "LoopLagStats"]


@dataclass(frozen=True, slots=True)
class LoopLagStats:
    """How late the event loop ran timers.

    Attributes:
        lag_avg: The mean lag of the samples, in seconds.
        lag_max: The largest lag sampled, in seconds.
    """

    lag_avg: float
    lag_max: float


class LoopLag:
    """Samples how late the event loop wakes up a sleeping task.

    A loop busy with blocking code, such as slow parsing or synchronous I/O, wakes
    timers late; the lag is how much later than asked. Each sample is a beat of the
    `LoopWatchdog`, if one is given, which watches the loop from `start()` to
    `stop()`.
    """

    def __init__(self, interval: float = 1.0, *, watchdog: LoopWatchdog | None = None) -> None:
        """Configure the sampling.

        Args:
            interval: Seconds between two samples. With a watchdog, they are at most
                a quarter of its threshold, so that blocks are measured to within a
                quarter of it.
            watchdog: Watches the loop for blocks, beating with each sample.

        Raises:
            ValueError: `interval` is not positive.
        """
        if interval <= 0:
            raise ValueError(f"interval must be positive, got {interval}")
        self._interval = interval if watchdog is None else min(interval, watchdog.threshold / 4)
        self._watchdog = watchdog
        self._sampling: asyncio.Task[None] | None = None
        self._samples = 0
        self._total = 0.0
        self._max = 0.0

    def stats(self) -> LoopLagStats:
        """Return the lag measured so far; zeros before the first sample."""
        average = self._total / self._samples if self._samples else 0.0
        return LoopLagStats(lag_avg=average, lag_max=self._max)

    def start(self) -> None:
        """Start sampling the running event loop, in a task; the watchdog, if any, watches it at once.

        The watchdog does not wait for the task to run, so code that blocks the loop
        right after, before the loop runs the task, is watched too.

        Raises:
            RuntimeError: The sampling was started before, or no event loop runs.
        """
        if self._sampling is not None:
            raise RuntimeError("a LoopLag samples only once")
        self._sampling = asyncio.get_running_loop().create_task(self._sample())
        if self._watchdog is not None:
            self._watchdog.start(self._interval)

    async def stop(self) -> None:
        """Stop sampling, and the watchdog, which counts a block it reported as over."""
        if self._sampling is None:
            return
        self._sampling.cancel()
        try:
            await asyncio.wait([self._sampling])
        finally:
            if self._watchdog is not None:
                self._watchdog.stop()

    async def _sample(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self._interval)
            lag = max(0.0, loop.time() - started - self._interval)
            self._samples += 1
            self._total += lag
            self._max = max(self._max, lag)
            if self._watchdog is not None:
                self._watchdog.beat(self._interval)
