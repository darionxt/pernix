from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.events import SpiderPaused, SpiderResumed

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.events import Event, EventBus
    from pernix.spider import Spider

__all__ = ["PauseCounts", "Pausing"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PauseCounts:
    """How long a Run was paused, read into its stats as `run.pauses` and `run.paused_seconds`.

    Attributes:
        pauses: How many times it was paused.
        paused_seconds: Seconds it spent paused, a pause in progress included.
    """

    pauses: int
    paused_seconds: float


class Pausing:
    """Carries out the pause and resume requests of one Run.

    While the Run is `held`, the Engine claims no Request and takes none from
    `start()` or `on_idle()`, waiting in `released()`; `holding()` waits for a pause
    instead. Between `begin()` and `end()`, each change is logged, counted, and
    queued as `SpiderPaused` or `SpiderResumed`, which `announce()` emits in the order
    requested. A pause holds the Run at once; a resume releases it once announced, so
    that no Event of the resumed Run comes before `SpiderResumed`. A pause requested
    before `begin()` takes effect then; a request after `end()` does nothing, and so
    does one that changes nothing. `end()` releases the Run for good, as a Drain or
    the end of the Run need.
    """

    def __init__(self, spider: Spider, events: EventBus, *, clock: Callable[[], float] = time.monotonic) -> None:
        """Configure the pausing.

        Args:
            spider: The Spider of the Run, which the Events name.
            events: Where `SpiderPaused` and `SpiderResumed` are emitted.
            clock: Measures how long the Run is paused, in seconds.
        """
        self._spider = spider
        self._events = events
        self._clock = clock
        self._paused = False
        self._released = asyncio.Event()
        self._released.set()
        self._held = asyncio.Event()
        self._changes: asyncio.Queue[Event] = asyncio.Queue()
        self._active = False
        self._ended = False
        self._pauses = 0
        self._paused_seconds = 0.0
        self._paused_since: float | None = None

    @property
    def paused(self) -> bool:
        """Whether the Run is paused, or will start paused; `False` once ended."""
        return self._paused

    @property
    def held(self) -> bool:
        """Whether the Run is held: from a pause until its resume is announced."""
        return not self._released.is_set()

    def pause(self) -> None:
        """Pause the Run, unless it is paused or ended."""
        if self._ended or self._paused:
            return
        self._paused = True
        self._released.clear()
        self._held.set()
        if self._active:
            self._start_pause()

    def resume(self) -> None:
        """Resume the Run, if it is paused and not ended."""
        if self._ended or not self._paused:
            return
        self._paused = False
        if not self._active:
            self._release()
            return
        paused_for = self._stop_clock()
        logger.info("Resuming the Run after %.1f s paused", paused_for)
        self._changes.put_nowait(SpiderResumed(self._spider, paused_for))

    def begin(self) -> None:
        """Start carrying out the requests, applying a pause requested before."""
        if self._ended or self._active:
            return
        self._active = True
        if self._paused:
            self._start_pause()

    def end(self) -> None:
        """Stop carrying out the requests, releasing the waits; `announce()` returns once the changes are emitted."""
        if self._ended:
            return
        self._ended = True
        self._active = False
        self._paused = False
        self._stop_clock()
        self._release()
        self._changes.shutdown()

    async def released(self) -> None:
        """Wait until the Run is not held: resumed and announced so, or ended."""
        while self.held:  # a pause right after the release holds it again before the wait returns
            await self._released.wait()

    async def holding(self) -> None:
        """Wait until the Run is held, as by a pause; while it is, return at once."""
        await self._held.wait()

    async def announce(self) -> None:
        """Emit the changes as `SpiderPaused` and `SpiderResumed`, in order, until `end()`; release a resumed Run."""
        while True:
            try:
                event = await self._changes.get()
            except asyncio.QueueShutDown:
                return
            await self._events.emit(event)
            if not self._paused and self._changes.empty():
                self._release()

    def counts(self) -> PauseCounts:
        """Count the pauses and the seconds spent paused so far, a pause in progress included.

        Returns:
            The counts, for the stats.
        """
        seconds = self._paused_seconds
        if self._paused_since is not None:
            seconds += self._clock() - self._paused_since
        return PauseCounts(self._pauses, round(seconds, 3))

    def _release(self) -> None:
        self._held.clear()
        self._released.set()

    def _start_pause(self) -> None:
        self._pauses += 1
        self._paused_since = self._clock()
        logger.info("Pausing the Run: no new Request is claimed until it is resumed")
        self._changes.put_nowait(SpiderPaused(self._spider))

    def _stop_clock(self) -> float:
        """Add the pause in progress, if any, to the time paused; return how long it lasted."""
        if self._paused_since is None:
            return 0.0
        paused_for = self._clock() - self._paused_since
        self._paused_seconds += paused_for
        self._paused_since = None
        return paused_for
