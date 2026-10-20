from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from pernix.control import RunControl
from pernix.stats import Stats

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.control import ControlSource, ControlState
    from pernix.shutdown import StopMode


class Told:
    """A source telling the states given, in turn, then the last one; an exception given is raised."""

    def __init__(self, *told: str | Exception) -> None:
        self._told = list(told)
        self.reads = 0
        self.read = asyncio.Event()

    def state(self) -> ControlState:
        self.reads += 1
        self.read.set()
        told = self._told.pop(0) if len(self._told) > 1 else self._told[0]
        if isinstance(told, Exception):
            raise told
        return told  # pyright: ignore[reportReturnType]  # a source may tell anything


class AsyncTold(Told):
    async def state(self) -> ControlState:  # pyright: ignore[reportIncompatibleMethodOverride]
        await asyncio.sleep(0)
        return super().state()


class Never:
    async def state(self) -> ControlState:
        await asyncio.Event().wait()
        return "running"


class Applied:
    """Records what a RunControl applies to the Run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def pause(self) -> None:
        self.calls.append("pause")

    def resume(self) -> None:
        self.calls.append("resume")

    def stop(self, mode: StopMode) -> None:
        self.calls.append(f"stop {mode}")


def _control(
    sources: Iterable[tuple[str, ControlSource]], *, interval: float = 60.0
) -> tuple[RunControl, Applied, Stats]:
    applied, stats = Applied(), Stats()
    control = RunControl(
        list(sources),
        interval=interval,
        pause=applied.pause,
        resume=applied.resume,
        stop=applied.stop,
        stats=stats,
    )
    return control, applied, stats


async def _poll(control: RunControl, times: int) -> None:
    for _ in range(times):
        await control.poll()


async def test_only_a_change_of_state_is_applied_starting_from_running() -> None:
    control, applied, stats = _control([("file", Told("running", "paused", "paused", "running", "running"))])

    await _poll(control, 5)

    assert applied.calls == ["pause", "resume"]
    assert (stats.get("control.state"), stats.get("control.failures")) == ("running", 0)


async def test_sync_and_async_sources_are_read() -> None:
    control, applied, _ = _control([("sync", Told("running")), ("async", AsyncTold("paused"))])

    await control.poll()

    assert applied.calls == ["pause"]


async def test_the_most_restrictive_state_told_wins() -> None:
    control, applied, stats = _control(
        [("a", Told("paused", "paused", "running")), ("b", Told("running", "drain")), ("c", Told("running"))]
    )

    await _poll(control, 3)

    assert applied.calls == ["pause", "stop drain"]
    assert stats.get("control.state") == "drain"


async def test_sources_of_the_same_label_are_told_apart(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="pernix")
    control, applied, _ = _control(
        [("Store", Told("force")), ("Store", Told("running")), ("Store", Told(OSError("down")))]
    )

    await control.poll()

    assert applied.calls == ["stop force"]
    assert "Control: force, as Store #1 tells" in caplog.text
    assert "Reading the control source Store #3 failed" in caplog.text


async def test_a_stop_cannot_be_undone_but_a_drain_escalates_to_a_force_stop() -> None:
    control, applied, _ = _control([("file", Told("drain", "running", "paused", "drain", "force", "running"))])

    await _poll(control, 6)

    assert applied.calls == ["stop drain", "stop force"]


async def test_a_failing_source_keeps_the_state_it_told_before_and_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="pernix")
    broken = OSError("Permission denied")
    control, applied, stats = _control([("file", Told("paused", broken, broken, ValueError("empty"), "running"))])

    await _poll(control, 5)

    assert applied.calls == ["pause", "resume"]
    assert stats.get("control.failures") == 3
    warnings = [record.getMessage() for record in caplog.records if record.levelno == logging.WARNING]
    assert warnings == [
        "Reading the control source file failed; keeping the state it told before, paused: OSError: Permission denied",
        "Reading the control source file failed; keeping the state it told before, paused: ValueError: empty",
    ]
    assert "The control source file reads again: running" in caplog.text


async def test_a_source_telling_something_else_is_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    control, applied, stats = _control([("custom", Told("stop"))])

    await control.poll()

    assert (applied.calls, stats.get("control.failures")) == ([], 1)
    assert "keeping the state it told before, running: it told 'stop', not a state" in caplog.text


async def test_a_source_slower_than_the_interval_is_a_failure(caplog: pytest.LogCaptureFixture) -> None:
    control, applied, stats = _control([("slow", Never()), ("file", Told("paused"))], interval=0.01)

    async with asyncio.timeout(5):
        await control.poll()

    assert (applied.calls, stats.get("control.failures")) == (["pause"], 1)
    assert "it took longer than control.interval (0.01 s)" in caplog.text


async def test_the_sources_are_read_on_opening_then_every_interval_until_closed() -> None:
    source = Told("paused")
    control, applied, _ = _control([("file", source)], interval=0.01)

    await control.open()
    assert (source.reads, applied.calls) == (1, ["pause"])
    source.read.clear()
    async with asyncio.timeout(5):
        await source.read.wait()
    await control.close()
    reads = source.reads
    await asyncio.sleep(0.05)

    assert reads >= 2
    assert source.reads == reads  # no read after closing
    assert applied.calls == ["pause"]


def test_the_interval_must_be_positive() -> None:
    with pytest.raises(ValueError, match="interval must be positive, got 0"):
        _control([], interval=0)
