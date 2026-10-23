from __future__ import annotations

import asyncio
import heapq
import itertools
import time
from collections import Counter
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from pernix.fetchers.fetcher_dispatch import is_rendered
from pernix.fingerprint import request_fingerprint
from pernix.frontiers.frontier import FrontierCounts, Lease, host_slot

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence

    from pernix.request import Request

__all__ = ["MemoryFrontier"]

type _State = Literal["ready", "delayed", "leased", "dead"]

type _Entry = tuple[int, int, str]
"""A ready Request in the queue of its slot: `(-priority, sequence, id)`, which orders it as claims go."""

type _Head = tuple[int, int, int, str]
"""The first ready Request of a slot, `(-priority, sequence, mark, slot)`; only the slot's latest mark is live."""


@dataclass(slots=True)
class _Item:
    id: str
    request: Request
    slot: str
    rendered: bool = False
    state: _State = "ready"
    attempts: int = 0
    """Failed attempts that count."""
    spent: dict[str, int] = field(default_factory=dict[str, int])
    """Retries under each retry budget."""
    token: int = 0
    expires_at: float = 0.0
    not_before: float = 0.0
    reason: str = ""


class _ReadyQueue:
    """The ready Requests of one kind, queued per slot, with a heap of the first Request of each slot.

    A claim goes through the slots by their first Requests, so it skips a slot at
    once, however many Requests wait in it. A Request that becomes the first of its
    slot ahead of another, by a higher priority, leaves the entry of that one in the
    heap stale, and the heap is rebuilt once it holds more stale entries than live ones.
    """

    def __init__(self) -> None:
        """Create an empty queue."""
        self._slots: dict[str, list[_Entry]] = {}  # a heap per slot with ready Requests
        self._heads: list[_Head] = []
        self._marks: dict[str, int] = {}  # the mark of the live entry of each slot in `_heads`
        self._next_marks = itertools.count()
        self._skipped: list[_Head] = []  # set aside by `first()` until `restore()`

    def push(self, slot: str, entry: _Entry) -> None:
        """Queue a ready Request.

        Args:
            slot: The slot of the Request.
            entry: The Request's entry.
        """
        queue = self._slots.setdefault(slot, [])
        heapq.heappush(queue, entry)
        if queue[0] == entry:  # the first of its slot now
            self._set_first(slot, entry)
            if len(self._heads) > 2 * len(self._marks):  # more stale entries than live ones
                self._heads = [head for head in self._heads if self._live(head)]
                heapq.heapify(self._heads)

    def first(self, skip_slots: Collection[str]) -> _Entry | None:
        """Find the first Request outside `skip_slots`, leaving it queued for `pop()`.

        The slots skipped on the way are set aside until `restore()`, so finding the
        next one does not go through them again.

        Args:
            skip_slots: The slots whose Requests to leave for later.

        Returns:
            The Request's entry, or `None` if every ready Request is in `skip_slots`.
        """
        while self._heads:
            _, _, _, slot = head = self._heads[0]
            if not self._live(head):
                heapq.heappop(self._heads)
            elif slot in skip_slots:
                self._skipped.append(heapq.heappop(self._heads))
            else:
                return self._slots[slot][0]
        return None

    def pop(self) -> str:
        """Take the Request the last `first()` found out of the queue; return its id.

        The queue must not change between the two.
        """
        _, _, _, slot = heapq.heappop(self._heads)
        queue = self._slots[slot]
        _, _, item_id = heapq.heappop(queue)
        if queue:
            self._set_first(slot, queue[0])
        else:
            del self._slots[slot], self._marks[slot]
        return item_id

    def restore(self) -> None:
        """Queue again the slots `first()` set aside."""
        for head in self._skipped:
            heapq.heappush(self._heads, head)
        self._skipped.clear()

    def _set_first(self, slot: str, entry: _Entry) -> None:
        """Make a Request the first of its slot in `_heads`, which leaves the slot's earlier entry stale."""
        self._marks[slot] = mark = next(self._next_marks)
        heapq.heappush(self._heads, (entry[0], entry[1], mark, slot))

    def _live(self, head: _Head) -> bool:
        """Whether an entry of `_heads` is the latest of its slot, rather than stale."""
        _, _, mark, slot = head
        return self._marks.get(slot) == mark


class MemoryFrontier:
    """A Frontier held in memory, implementing the full Lease state machine.

    It is lost when the process ends, so it suits Runs that need no resuming. Done
    Requests are forgotten, except for their Fingerprints; Dead Requests are kept.
    Ready Requests queue per slot, and rendered ones apart from the others, so a
    claim skips a slot at once, however many Requests wait in it, and a claim of one
    kind never goes through the Requests of the other.
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
        self._ready = {False: _ReadyQueue(), True: _ReadyQueue()}  # by whether rendered
        self._delayed: list[tuple[float, int, str]] = []  # (not_before, sequence, id)
        self._expiries: list[tuple[float, int, str]] = []  # (expires_at, token, id)
        self._counts: Counter[str] = Counter()
        self._ids = itertools.count(1)
        self._sequences = itertools.count()
        self._tokens = itertools.count(1)
        self._changed = asyncio.Event()

    async def add(self, requests: Sequence[Request]) -> list[bool]:
        """Deduplicate and store Requests; see `Frontier.add()`."""
        # Compute every Fingerprint and slot first, so a failing one leaves nothing half-added.
        prepared = [(None if r.dont_filter else self._key(r), self._slot_of(r), is_rendered(r)) for r in requests]
        self._refresh()  # Requests that became ready before these queue ahead of them
        added: list[bool] = []
        for request, (key, slot, rendered) in zip(requests, prepared, strict=True):
            if key is not None:
                if key in self._seen:
                    added.append(False)
                    continue
                self._seen.add(key)
            item = _Item(str(next(self._ids)), request, slot, rendered)
            self._items[item.id] = item
            self._counts["ready"] += 1
            self._push_ready(item)
            added.append(True)
        if any(added):
            self._notify()
        return added

    async def claim(
        self,
        worker_id: str,
        limit: int,
        ttl: float,
        skip_slots: Collection[str] = frozenset(),
        *,
        rendered: bool | None = None,
    ) -> list[Lease]:
        """Lease ready Requests; see `Frontier.claim()`."""
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        _check_ttl(ttl)
        now = self._refresh()
        due = self._next_due()
        queues = list(self._ready.values()) if rendered is None else [self._ready[rendered]]
        leases: list[Lease] = []
        try:
            while len(leases) < limit:
                firsts = [(entry, queue) for queue in queues if (entry := queue.first(skip_slots)) is not None]
                if not firsts:
                    break
                _, ready = min(firsts, key=lambda first: first[0])
                leases.append(self._lease(self._items[ready.pop()], now + ttl))
        finally:
            for queue in queues:
                queue.restore()  # the slots skipped
        if leases and (due is None or now + ttl < due):
            self._notify()  # waiters wake up by the earliest deadline, which is now sooner
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

    async def nack(
        self,
        lease: Lease,
        *,
        delay: float = 0.0,
        count_attempt: bool = True,
        reason: str = "",
        budget: str | None = None,
        request: Request | None = None,
    ) -> bool:
        """Return a leased Request; see `Frontier.nack()`."""
        now = self._refresh()
        item = self._leased(lease)
        if item is None:
            return False
        if request is not None:
            item.request, item.rendered = request, is_rendered(request)
        if budget is None:
            item.attempts += count_attempt
        else:
            item.spent = {**item.spent, budget: item.spent.get(budget, 0) + 1}  # Leases handed out keep theirs
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
        due = self._next_due()
        try:
            async with asyncio.timeout(None if due is None else max(0.0, due - now)):
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

    def _next_due(self) -> float | None:
        """When the next delay passes or Lease expires, if any; stale entries may make it early."""
        due = [entry[0] for entry in (self._delayed[:1] + self._expiries[:1])]
        return min(due) if due else None

    def _leased(self, lease: Lease) -> _Item | None:
        """The item a Lease is valid for, or `None` for a stale Lease."""
        item = self._items.get(lease.id)
        if item is None or item.state != "leased" or item.token != lease.token:
            return None
        return item

    def _lease(self, item: _Item, expires_at: float) -> Lease:
        item.token = next(self._tokens)
        item.expires_at = expires_at
        self._move(item, "leased")
        heapq.heappush(self._expiries, (item.expires_at, item.token, item.id))
        attempt = item.attempts + 1
        return Lease(item.id, item.token, attempt, item.expires_at, item.slot, item.request, item.rendered, item.spent)

    def _push_ready(self, item: _Item) -> None:
        self._ready[item.rendered].push(item.slot, (-item.request.priority, next(self._sequences), item.id))

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
