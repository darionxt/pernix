"""Reading the control sources of a Run, and pausing, resuming, or stopping it as they tell."""

from __future__ import annotations

import asyncio
import inspect
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

from pernix.control.control_source import CONTROL_STATES

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from pernix.control.control_source import ControlSource, ControlState
    from pernix.shutdown import StopMode
    from pernix.stats import Stats

__all__ = ["RunControl"]

logger = logging.getLogger(__name__)

_RANKS: dict[str, int] = {state: rank for rank, state in enumerate(CONTROL_STATES)}
_STOPS: frozenset[ControlState] = frozenset({"drain", "force"})


class RunControl:
    """Reads the control sources of a Run, and pauses, resumes, or stops it as they tell.

    `open()` reads them once, so that a Run can start paused, or stop before it
    starts, then every `interval` seconds until `close()`. The states they tell are
    combined, the most restrictive winning: `force`, then `drain`, then `paused`, then
    `running`. Only a change of the combined state is applied, starting from
    `running`: `running` resumes the Run, `paused` pauses it, and `drain` and `force`
    stop it, as `stop()` does. So a Run that code pauses or resumes stays so until the
    sources tell another state. A stop cannot be undone: once one is applied, only a
    Force Stop after a Drain is.

    A source that raises, takes longer than `interval`, or tells something other
    than a state is logged, once until it tells something else, and the state it
    told before is kept; one never read yet counts as `running`. The stats hold
    `control.state`, the combined state last read, and `control.failures`, the
    reads that failed.
    """

    def __init__(
        self,
        sources: Sequence[tuple[str, ControlSource]],
        *,
        interval: float,
        pause: Callable[[], None],
        resume: Callable[[], None],
        stop: Callable[[StopMode], None],
        stats: Stats,
    ) -> None:
        """Configure the reading.

        Args:
            sources: The sources, each with a label naming it in the log, in order.
            interval: Seconds between two reads, and the time each read may take.
            pause: Pauses the Run.
            resume: Resumes the Run.
            stop: Stops the Run, with a Drain or a Force Stop.
            stats: Where the state and the failures are counted.

        Raises:
            ValueError: `interval` is not positive.
        """
        if interval <= 0:
            raise ValueError(f"interval must be positive, got {interval}")
        self._sources = tuple(sources)
        self._interval = interval
        self._pause = pause
        self._resume = resume
        self._stop = stop
        self._stats = stats
        self._told: dict[str, ControlState] = dict.fromkeys((label for label, _ in self._sources), "running")
        self._failing: dict[str, str] = {}
        """The failure each failing source was last logged with, by its label."""
        self._applied: ControlState = "running"
        self._polling: asyncio.Task[None] | None = None
        stats.set("control.failures", 0)

    async def open(self) -> None:
        """Read the sources and apply their state, then read them every `interval` seconds."""
        await self.poll()
        self._polling = asyncio.create_task(self._periodically(), name="pernix-control")

    async def close(self) -> None:
        """Stop reading the sources."""
        if self._polling is not None:
            self._polling.cancel()
            with suppress(asyncio.CancelledError):
                await self._polling
            self._polling = None

    async def poll(self) -> None:
        """Read every source at once, and apply the combined state if it changed."""
        await asyncio.gather(*(self._read(label, source) for label, source in self._sources))
        state = max(self._told.values(), key=_RANKS.__getitem__)
        self._stats.set("control.state", state)
        if state == self._applied or (self._applied in _STOPS and _RANKS[state] <= _RANKS[self._applied]):
            return  # unchanged, or a stop that cannot be undone
        self._applied = state
        by = ", ".join(label for label, told in self._told.items() if told == state)
        logger.info("Control: %s, as %s tells", state, by)
        match state:
            case "running":
                self._resume()
            case "paused":
                self._pause()
            case "drain" | "force":
                self._stop(state)

    async def _periodically(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.poll()
            except Exception:
                logger.exception("Applying the control state failed")

    async def _read(self, label: str, source: ControlSource) -> None:
        """Read one source, keeping the state it told before if that fails."""
        try:
            async with asyncio.timeout(self._interval):
                told = source.state()
                if inspect.isawaitable(told):
                    told = await told
        except TimeoutError:
            self._failed(label, f"it took longer than control.interval ({self._interval} s)")
            return
        except Exception as exc:
            self._failed(label, f"{type(exc).__name__}: {exc}")
            return
        if told not in CONTROL_STATES:
            self._failed(label, f"it told {told!r}, not a state")
            return
        if self._failing.pop(label, None) is not None:
            logger.info("The control source %s reads again: %s", label, told)
        self._told[label] = told

    def _failed(self, label: str, failure: str) -> None:
        self._stats.inc("control.failures")
        if self._failing.get(label) == failure:
            return
        self._failing[label] = failure
        logger.warning(
            "Reading the control source %s failed; keeping the state it told before, %s: %s",
            label,
            self._told[label],
            failure,
        )
