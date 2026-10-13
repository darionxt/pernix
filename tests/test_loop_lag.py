from __future__ import annotations

import asyncio
import time

import pytest

from pernix.stats import LoopLag


async def test_loop_lag_measures_how_late_timers_fire() -> None:
    lag = LoopLag(interval=0.01)
    sampling = asyncio.create_task(lag.run())
    await asyncio.sleep(0.005)

    time.sleep(0.06)  # blocks the event loop  # noqa: ASYNC251
    await asyncio.sleep(0.03)
    sampling.cancel()

    assert lag.stats().lag_max >= 0.04
    assert 0 < lag.stats().lag_avg <= lag.stats().lag_max


def test_loop_lag_starts_at_zero_and_needs_a_positive_interval() -> None:
    assert LoopLag().stats() == LoopLag().stats()
    assert (LoopLag().stats().lag_avg, LoopLag().stats().lag_max) == (0.0, 0.0)
    with pytest.raises(ValueError, match="interval must be positive"):
        LoopLag(interval=0)
