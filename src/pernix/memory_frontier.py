from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import Counter
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Literal

from pernix.fingerprint import request_fingerprint
from pernix.frontier import FrontierCounts, Lease, host_slot

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from pernix.request import Request

__all__ = ["MemoryFrontier"]

type _State = Literal["ready", "delayed", "leased", "dead"]


@dataclass(slots=True)
class _Item:
    id: str
    request: Request
    slot: str
    state: _State = "ready"
    sequence: int = 0
    """Orders ready Requests of one priority; a newer heap entry makes older ones stale."""
    attempts: int = 0
    """Failed attempts that count."""
    token: int = 0
    expires_at: float = 0.0
    not_before: float = 0.0
    reason: str = ""


class MemoryFrontier:
    """A Frontier held in memory, implementing the full Lease state machine.

    It is lost when the process ends, so it suits Runs that need no resuming. Done
    Requests are forgotten, except for their Fingerprints; Dead Requests are kept.
    """

    def __init__(
        self,
        *,
        fingerprint: Callable[[Request], str] = request_fingerprint,
        slot_of: Callable[[Request], str] = host_slot,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Create an empty Frontier.

        Args:
            fingerprint: Computes the Fingerprint of Requests without their own.
            slot_of: Computes the concurrency slot of a Request.
            clock: The clock for Lease expiry and delays, in seconds.
        """
        self._fingerprint = fingerprint
        self._slot_of = slot_of
        self._clock = clock
        self._items: dict[str, _Item] = {}
        self._seen: set[str] = set()
        self._ready: list[tuple[int, int, str]] = []  # (-priority, sequence, id)
        self._delayed: list[tuple[float, int, str]] = []  # (not_before, sequence, id)
        self._expiries: list[tuple[float, int, str]] = []  # (expires_at, token, id)
        self._counts: Counter[str] = Counter()
        self._ids = itertools.count(1)
        self._sequences = itertools.count()
        self._tokens = itertools.count(1)
        self._changed = asyncio.Event()

    async def add(self, requests: Sequence[Request]) -> list[bool]:
        """Deduplicate and store Requests; see `Frontier.add()`."""
        # Compute every Fingerprint first, so a failing one leaves nothing half-added.
        keys = [None if r.dont_filter else self._key(r) for r in requests]
        added: list[bool] = []
        for request, key in zip(requests, keys, strict=True):
            if key is not None:
                if key in self._seen:
                    added.append(False)
                    continue
                self._seen.add(key)
            item = _Item(str(next(self._ids)), request, self._slot_of(request))
            self._items[item.id] = item
            self._counts["ready"] += 1
            self._push_ready(item)
            added.append(True)
        if any(added):
            self._notify()
        return added

    async def claim(
        self, worker_id: str, limit: int, ttl: float, skip_slots: Collection[str] = frozenset()
    ) -> list[Lease]:
        """Lease ready Requests; see `Frontier.claim()`."""
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        _check_ttl(ttl)
        now = self._refresh()
        leases: list[Lease] = []
        skipped: list[tuple[int, int, str]] = []
        while self._ready and len(leases) < limit:
            entry = heapq.heappop(self._ready)
            item = self._items.get(entry[2])
            if item is None or item.state != "ready" or item.sequence != entry[1]:
                continue  # stale entry
            if item.slot in skip_slots:
                skipped.append(entry)
                continue
            item.token = next(self._tokens)
            item.expires_at = now + ttl
            self._move(item, "leased")
            heapq.heappush(self._expiries, (item.expires_at, item.token, item.id))
            leases.append(Lease(item.id, item.token, item.attempts + 1, item.expires_at, item.slot, item.request))
        for entry in skipped:
            heapq.heappush(self._ready, entry)
        return leases

    async def renew(self, lease: Lease, ttl: float) -> Lease | None:
        """Extend a Lease; see `Frontier.renew()`."""
        _check_ttl(ttl)
        now = self._refresh()
        item = self._leased(lease)
        if item is None:
            return None
        item.expires_at = now + ttl
        heapq.heappush(self._expiries, (item.expires_at, item.token, item.id))
        return replace(lease, expires_at=item.expires_at)

    async def ack(self, lease: Lease) -> bool:
        """Mark a leased Request as done; see `Frontier.ack()`."""
        self._refresh()
        item = self._leased(lease)
        if item is None:
            return False
        self._counts["leased"] -= 1
        self._counts["done"] += 1
        del self._items[item.id]
        self._notify()
        return True

    async def nack(self, lease: Lease, *, delay: float = 0.0, count_attempt: bool = True, reason: str = "") -> bool:
        """Return a leased Request; see `Frontier.nack()`."""
        now = self._refresh()
        item = self._leased(lease)
        if item is None:
            return False
        item.attempts += count_attempt
        item.reason = reason
        if delay > 0:
            item.not_before = now + delay
            self._move(item, "delayed")
            heapq.heappush(self._delayed, (item.not_before, next(self._sequences), item.id))
        else:
            self._move(item, "ready")
            self._push_ready(item)
        self._notify()
        return True

    async def dead(self, lease: Lease, reason: str) -> bool:
        """Keep a leased Request as a Dead Request; see `Frontier.dead()`."""
        self._refresh()
        item = self._leased(lease)
        if item is None:
            return False
        item.reason = reason
        self._move(item, "dead")
        self._notify()
        return True

    async def counts(self) -> FrontierCounts:
        """Count the Requests in each state; see `Frontier.counts()`."""
        self._refresh()
        counts = self._counts
        return FrontierCounts(counts["ready"], counts["delayed"], counts["leased"], counts["done"], counts["dead"])

    async def wait(self) -> None:
        """Wait for a change, or until the next delay or Lease is due; see `Frontier.wait()`."""
        changed = self._changed  # a change made by the refresh below sets this one
        now = self._refresh()
        due = [entry[0] for entry in (self._delayed[:1] + self._expiries[:1])]
        try:
            async with asyncio.timeout(max(0.0, min(due) - now) if due else None):
                await changed.wait()
        except TimeoutError:
            pass

    async def close(self) -> None:
        """Do nothing: the Requests stay in memory until the Frontier is discarded."""

    def _key(self, request: Request) -> str:
        return request.fingerprint if request.fingerprint is not None else self._fingerprint(request)

    def _refresh(self) -> float:
        """Make passed delays ready and expired Leases claimable again; return the time."""
        now = self._clock()
        changed = False
        while self._delayed and self._delayed[0][0] <= now:
            item = self._items[heapq.heappop(self._delayed)[2]]  # delayed Requests stay delayed until due
            self._move(item, "ready")
            self._push_ready(item)
            changed = True
        while self._expiries and self._expiries[0][0] <= now:
            expires_at, token, item_id = heapq.heappop(self._expiries)
            item = self._items.get(item_id)
            if item is None or item.state != "leased" or (item.token, item.expires_at) != (token, expires_at):
                continue  # settled or renewed since
            item.attempts += 1
            self._move(item, "ready")
            self._push_ready(item)
            changed = True
        if changed:
            self._notify()
        return now

    def _leased(self, lease: Lease) -> _Item | None:
        """The item a Lease is valid for, or `None` for a stale Lease."""
        item = self._items.get(lease.id)
        if item is None or item.state != "leased" or item.token != lease.token:
            return None
        return item

    def _push_ready(self, item: _Item) -> None:
        item.sequence = next(self._sequences)
        heapq.heappush(self._ready, (-item.request.priority, item.sequence, item.id))

    def _move(self, item: _Item, state: _State) -> None:
        self._counts[item.state] -= 1
        self._counts[state] += 1
        item.state = state

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


def _check_ttl(ttl: float) -> None:
    if ttl <= 0:
        raise ValueError(f"ttl must be positive, got {ttl}")
