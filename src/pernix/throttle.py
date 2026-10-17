"""Per-domain politeness: concurrency slots, spacing, rate limiting, and cooldowns.

Every fetch holds the slot of its Request, the host by default, from just before the
download chain until the Response arrives. A slot admits a new fetch while fewer than
`domain.concurrency` are in progress, `domain.delay` has passed since the last start,
the GCRA rate allows it, and the slot is not cooling down. A 429 or 503 Response with
`Retry-After` cools its whole slot down for that long.

A fetch nested in another of the same slot, in the same task, such as an Inline Fetch
a download Middleware makes, shares the outer fetch's place instead of waiting for
another, which the outer fetch could hold forever. It still waits for the delay and
the rate, and is refused while the slot cools down.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pernix.retry_policy import retry_after_seconds

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Generator

    from pernix.response import Response
    from pernix.settings import DomainSettings

__all__ = ["COOLDOWN_STATUSES", "SESSION_COOLDOWN_STATUSES", "Throttle", "ThrottleStats"]

COOLDOWN_STATUSES = frozenset({429, 503})
"""Statuses whose `Retry-After` pauses the whole slot."""

SESSION_COOLDOWN_STATUSES = frozenset({503})
"""Statuses whose `Retry-After` pauses the whole slot of a fetch under a Session: a 429 cools the Session down."""

_held: ContextVar[frozenset[tuple[Throttle, str, asyncio.Task[Any] | None]]] = ContextVar(
    "pernix_held_slots", default=frozenset()
)
"""The slots held by the fetches the current code runs inside of, with the task holding each."""


@dataclass(slots=True)
class ThrottleStats:
    """Counters of a Throttle.

    Attributes:
        waits: Times fetches waited for their slot after their Request was claimed:
            for a place, and for the delay or the rate.
        wait_seconds: The total time those fetches waited.
        cooldowns: Times a slot started or extended a cooldown.
        refused: Fetches refused because their slot was cooling down.
    """

    waits: int = 0
    wait_seconds: float = 0.0
    cooldowns: int = 0
    refused: int = 0


@dataclass(slots=True)
class _Slot:
    active: int = 0
    next_start: float = 0.0
    """The earliest start `domain.delay` allows."""
    tat: float = 0.0
    """GCRA's theoretical arrival time."""
    cool_until: float = 0.0


class Throttle:
    """Tracks the fetches in progress per slot and when each slot may start the next."""

    def __init__(
        self,
        settings: DomainSettings,
        *,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
    ) -> None:
        """Configure the Throttle.

        Args:
            settings: The per-domain settings.
            clock: A monotonic clock, in seconds.
            rng: The source of delay jitter.
        """
        self._settings = settings
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311  # jitter, not cryptography
        self._interval = 1.0 / settings.rate if settings.rate else 0.0
        self._tolerance = self._interval * (settings.burst - 1)
        self._slots: dict[str, _Slot] = {}
        self._changed = asyncio.Event()
        self.stats = ThrottleStats()

    def blocked(self) -> frozenset[str]:
        """Return the slots that cannot start a fetch now, for `Frontier.claim(skip_slots=...)`."""
        now = self._clock()
        blocked: set[str] = set()
        for name, slot in list(self._slots.items()):
            if slot.active >= self._settings.concurrency or self._ready_at(slot) > now:
                blocked.add(name)
            elif slot.active == 0 and max(slot.next_start, slot.tat, slot.cool_until) <= now:
                del self._slots[name]  # a fresh slot would behave the same
        return frozenset(blocked)

    def cooldown_left(self, slot: str) -> float:
        """Return the seconds until a slot's cooldown ends; 0 if it is not cooling down.

        Args:
            slot: The slot.

        Returns:
            The seconds left.
        """
        state = self._slots.get(slot)
        return 0.0 if state is None else max(0.0, state.cool_until - self._clock())

    async def acquire(self, slot: str, *, shared: bool = False) -> bool:
        """Start a fetch in a slot, waiting until the slot admits it: `admit()`, then `pace()`.

        Args:
            slot: The slot of the Request.
            shared: The fetch shares the place of a fetch it is nested in, which
                `holds()` tells: it waits for the delay and the rate, but not for a free
                place, and takes none, so it must not `release()` the slot.

        Returns:
            `True` once the fetch may start; `False` at once if the slot is cooling
            down, so the caller can return the Request instead of holding it.
        """
        if not await self.admit(slot, shared=shared):
            return False
        if await self.pace(slot):
            return True
        if not shared:
            self.release(slot)
        return False

    async def admit(self, slot: str, *, shared: bool = False) -> bool:
        """Take a place in a slot for a fetch, waiting until one is free; the delay and the rate come with `pace()`.

        A fetch that waits for something else before it starts, such as a Session,
        takes its place first and is paced just before it starts, so it never holds
        what another fetch waits for while it waits for a place, and its delay is
        measured from its actual start.

        Args:
            slot: The slot of the Request.
            shared: The fetch shares the place of a fetch it is nested in: it takes
                none, and must not `release()` the slot.

        Returns:
            `True` once it holds a place; `False` at once if the slot is cooling down.
        """
        started = self._clock()
        waited = False
        while True:
            state = self._slots.setdefault(slot, _Slot())  # blocked() may forget idle slots meanwhile
            if state.cool_until > self._clock():
                self.stats.refused += 1
                return False
            if shared or state.active < self._settings.concurrency:
                break
            waited = True
            await self._wait(None)
        if not shared:
            state.active += 1
        self._count_wait(waited, started)
        return True

    async def pace(self, slot: str) -> bool:
        """Wait until a slot's delay and rate let a fetch that holds a place there start, and start it.

        Args:
            slot: The slot of the Request.

        Returns:
            `True` once the fetch may start; `False` at once if the slot is cooling
            down, when the caller still holds its place and must `release()` it.
        """
        started = self._clock()
        waited = False
        while True:
            state = self._slots.setdefault(slot, _Slot())
            now = self._clock()
            if state.cool_until > now:
                self.stats.refused += 1
                return False
            ready_at = self._ready_at(state)
            if ready_at <= now:
                break
            waited = True
            await self._wait(ready_at - now)
        self._count_wait(waited, started)
        state.next_start = now + self._delay()
        if self._interval:
            state.tat = max(state.tat, now) + self._interval
        return True

    @contextmanager
    def holding(self, slot: str) -> Generator[None]:
        """Mark the code inside the block as holding a slot, in the current task.

        A fetch wraps its download in this once `acquire()` gave it the slot, so that
        the fetches nested in it share the slot: see `holds()`.

        Args:
            slot: The slot.

        Yields:
            Nothing; the block runs holding the slot.
        """
        token = _held.set(_held.get() | {(self, slot, asyncio.current_task())})
        try:
            yield
        finally:
            _held.reset(token)

    def holds(self, slot: str) -> bool:
        """Whether the current task holds a slot already, through a fetch it is nested in.

        Such a fetch shares the place of the outer one, with `acquire(slot,
        shared=True)`, rather than wait for another. Tasks the outer fetch starts do not
        share it, so they cannot run past `domain.concurrency`.

        Args:
            slot: The slot.

        Returns:
            Whether it is held.
        """
        return (self, slot, asyncio.current_task()) in _held.get()

    def release(self, slot: str) -> None:
        """End a fetch that `acquire()` started.

        Args:
            slot: The slot.
        """
        self._slots[slot].active -= 1
        self._notify()

    def observe(self, slot: str, response: Response, *, statuses: Collection[int] = COOLDOWN_STATUSES) -> None:
        """Cool a slot down if a Response asks its domain to wait.

        Args:
            slot: The slot the Response was fetched in.
            response: The Response; one of `statuses` with `Retry-After` starts the
                cooldown.
            statuses: The statuses that pause the whole slot; under a Session, a 429
                cools only the Session down, so only 503 pauses the slot.
        """
        if response.status not in statuses:
            return
        seconds = retry_after_seconds(response.headers.get("Retry-After"), now=dt.datetime.now(dt.UTC))
        if seconds is None or seconds <= 0:
            return
        state = self._slots.setdefault(slot, _Slot())
        state.cool_until = max(state.cool_until, self._clock() + min(seconds, self._settings.cooldown_max))
        self.stats.cooldowns += 1
        self._notify()

    async def wait(self) -> None:
        """Wait until a fetch ends, a cooldown starts, or a blocked slot may start again."""
        now = self._clock()
        waiting = [
            self._ready_at(slot) - now
            for slot in self._slots.values()
            if slot.active < self._settings.concurrency and self._ready_at(slot) > now
        ]
        await self._wait(min(waiting) if waiting else None)

    def _ready_at(self, slot: _Slot) -> float:
        """When the slot may start a fetch, concurrency aside."""
        return max(slot.next_start, slot.tat - self._tolerance, slot.cool_until)

    def _count_wait(self, waited: bool, started: float) -> None:
        if waited:
            self.stats.waits += 1
            self.stats.wait_seconds += self._clock() - started

    def _delay(self) -> float:
        delay, jitter = self._settings.delay, self._settings.delay_jitter
        return delay * self._rng.uniform(1.0 - jitter, 1.0 + jitter) if jitter else delay

    async def _wait(self, at_most: float | None) -> None:
        """Wait for a change, or for `at_most` seconds."""
        changed = self._changed
        try:
            async with asyncio.timeout(at_most):
                await changed.wait()
        except TimeoutError:
            pass

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()
