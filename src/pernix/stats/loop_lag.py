from __future__ import annotations

import asyncio
from dataclasses import dataclass

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
    timers late; the lag is how much later than asked.
    """

    def __init__(self, interval: float = 1.0) -> None:
        """Configure the sampling.

        Args:
            interval: Seconds between two samples.

        Raises:
            ValueError: `interval` is not positive.
        """
        if interval <= 0:
            raise ValueError(f"interval must be positive, got {interval}")
        self._interval = interval
        self._samples = 0
        self._total = 0.0
        self._max = 0.0

    def stats(self) -> LoopLagStats:
        """Return the lag measured so far; zeros before the first sample."""
        average = self._total / self._samples if self._samples else 0.0
        return LoopLagStats(lag_avg=average, lag_max=self._max)

    async def run(self) -> None:
        """Sample until cancelled."""
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()
            await asyncio.sleep(self._interval)
            lag = max(0.0, loop.time() - started - self._interval)
            self._samples += 1
            self._total += lag
            self._max = max(self._max, lag)
