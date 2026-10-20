from __future__ import annotations

import asyncio
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from functools import partial
from typing import TYPE_CHECKING

from pernix.fetchers.fetcher_dispatch import is_rendered
from pernix.fingerprint import request_fingerprint
from pernix.frontiers.frontier import host_slot
from pernix.frontiers.sqlite_store import FrontierStore, RequestRow
from pernix.request_codec import decode_request, encode_request

if TYPE_CHECKING:
    from collections.abc import Callable, Collection, Sequence
    from concurrent.futures import Future

    from pernix.frontiers.frontier import FrontierCounts, Lease
    from pernix.request import Request
    from pernix.spider import Spider

__all__ = ["SQLiteFrontier"]


class SQLiteFrontier:
    """A Frontier stored in one SQLite database, so that a Run can be resumed where it stopped.

    It runs the Lease state machine of `MemoryFrontier` and keeps everything it
    holds when it is closed or its process dies: ready, delayed, leased, and Dead
    Requests, with their attempts, retry budgets, Lease tokens, deadlines, and the
    reasons they were returned or failed for. Done Requests are deleted, but their
    Fingerprints are kept, so deduplication spans the whole Run. Each operation is
    one transaction of a database in WAL mode, so a process killed at any moment
    leaves every Request in exactly one state; a machine that loses power may lose
    the last transactions, leaving the database as it was a moment earlier.

    Requests are stored with `pernix.request_codec`: Callbacks, errbacks, and the
    page methods of a `Render` by the names of the Spider's methods, and `kwargs`,
    `meta`, and `ext` as JSON-compatible data. Deadlines are on the wall clock, so
    they hold in a later process.

    The database is used only from a thread of the Frontier's own, so a slow disk
    never stalls the event loop. It is opened by the first operation, and created if
    it is missing, readable by its owner only, as Requests carry cookies, tokens,
    and proxy credentials. An operation once called completes even if its caller is
    cancelled, and a claim cancelled gives back the Requests it leased, without
    counting an attempt. `wait()` returns on the changes made through this Frontier
    and when the next deadline falls due, so one process at a time should use a
    database. Dead Requests can be inspected in the table `requests`, with their
    `url` and `reason`.

    Example:
        >>> frontier = SQLiteFrontier("run/frontier.db", spider)
        >>> await frontier.add([Request("https://example.com/")])
        [True]
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        spider: Spider,
        *,
        fingerprint: Callable[[Request], str] = request_fingerprint,
        slot_of: Callable[[Request], str] = host_slot,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Configure the Frontier; the database is opened by the first operation.

        Args:
            path: The database file, in a directory that exists.
            spider: The Spider the Callbacks of the Requests belong to, which stores
                and restores them by name.
            fingerprint: Computes the Fingerprint of Requests without their own.
            slot_of: Computes the concurrency slot of a Request.
            clock: The wall clock for Lease expiry and delays, in seconds since the
                epoch, which deadlines stored in an earlier process are on.
        """
        self._spider = spider
        self._fingerprint = fingerprint
        self._slot_of = slot_of
        self._clock = clock
        self._store = FrontierStore(os.fspath(path), partial(decode_request, spider=spider))
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-frontier")
        self._changed = asyncio.Event()
        self._closed = False

    async def add(self, requests: Sequence[Request]) -> list[bool]:
        """Deduplicate and store Requests; see `Frontier.add()`.

        Raises:
            pernix.errors.SerializationError: A Request cannot be stored, as
                `encode_request()` tells, or the database cannot be read by this
                Pernix; none of the Requests is added.
        """
        # Prepare every Request first, so one that fails leaves nothing of the batch added.
        rows = [
            self._row(request, None if request.dont_filter else self._key(request), self._slot_of(request))
            for request in requests
        ]
        return await self._call(lambda now: self._store.add(now, rows))

    async def claim(
        self,
        worker_id: str,
        limit: int,
        ttl: float,
        skip_slots: Collection[str] = frozenset(),
        *,
        rendered: bool | None = None,
    ) -> list[Lease]:
        """Lease ready Requests; see `Frontier.claim()`.

        Raises:
            pernix.errors.SerializationError: A Request to lease cannot be restored,
                as `decode_request()` tells, such as one whose Callback is no
                longer a method of the Spider; nothing is leased, and it stays ready.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        _check_ttl(ttl)
        skipped = frozenset(skip_slots)
        claimed = self._submit(lambda now: self._store.claim(now, worker_id, limit, ttl, skipped, rendered))
        try:
            return await asyncio.shield(asyncio.wrap_future(claimed))
        except asyncio.CancelledError:
            if not self._closed:  # the claim goes on in the thread; what it leases goes back after it
                self._submit(lambda now: self._release(now, claimed))
            raise

    async def renew(self, lease: Lease, ttl: float) -> Lease | None:
        """Extend a Lease; see `Frontier.renew()`."""
        _check_ttl(ttl)
        expires_at = await self._call(lambda now: self._store.renew(now, lease, ttl))
        return None if expires_at is None else replace(lease, expires_at=expires_at)

    async def ack(self, lease: Lease) -> bool:
        """Mark a leased Request as done, keeping its Fingerprint; see `Frontier.ack()`."""
        return await self._call(lambda now: self._store.ack(now, lease))

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
        """Return a leased Request; see `Frontier.nack()`.

        Raises:
            pernix.errors.SerializationError: `request` cannot be stored, as
                `encode_request()` tells; nothing changed.
        """
        replacement = None if request is None else self._row(request, None, lease.slot)
        return await self._call(
            lambda now: self._store.nack(
                now,
                lease,
                delay=delay,
                count_attempt=count_attempt,
                reason=reason,
                budget=budget,
                replacement=replacement,
            )
        )

    async def dead(self, lease: Lease, reason: str) -> bool:
        """Keep a leased Request as a Dead Request, with the reason; see `Frontier.dead()`."""
        return await self._call(lambda now: self._store.dead(now, lease, reason))

    async def counts(self) -> FrontierCounts:
        """Count the Requests in each state, done ones included; see `Frontier.counts()`."""
        return await self._call(self._store.counts)

    async def wait(self) -> None:
        """Wait for a change made through this Frontier, or for the next deadline; see `Frontier.wait()`."""
        changed = self._changed  # a change the operation below makes sets this one
        due = await self._call(self._store.next_due)
        try:
            async with asyncio.timeout(None if due is None else max(0.0, due - self._clock())):
                await changed.wait()
        except TimeoutError:
            pass

    async def close(self) -> None:
        """Close the database once the operations called before are done; closing again does nothing.

        Stored Requests are never deleted. Operations called afterwards raise
        `RuntimeError`.
        """
        if self._closed:
            return
        self._closed = True
        closing = self._executor.submit(self._store.close)
        self._executor.shutdown(wait=False)  # its thread ends once the database is closed
        await asyncio.shield(asyncio.wrap_future(closing))

    def _key(self, request: Request) -> str:
        return request.fingerprint if request.fingerprint is not None else self._fingerprint(request)

    def _row(self, request: Request, fingerprint: str | None, slot: str) -> RequestRow:
        """Prepare a Request for the database, on the event loop, where its Fingerprint and slot were computed."""
        data = encode_request(request, self._spider)
        return RequestRow(fingerprint, slot, is_rendered(request), request.priority, request.url, data)

    async def _call[T](self, operation: Callable[[float], T]) -> T:
        """Run an operation of the store at the time now; it completes even if the caller is cancelled."""
        return await asyncio.shield(asyncio.wrap_future(self._submit(operation)))

    def _submit[T](self, operation: Callable[[float], T]) -> Future[T]:
        """Queue an operation of the store at the time now, waking the waiters once it changed something.

        Operations run one at a time, in the order they are submitted, in the
        Frontier's thread and its own context, so the Requests restored there take
        nothing from the caller's, such as the page a Callback handles.
        """
        if self._closed:
            raise RuntimeError("the Frontier is closed")
        loop = asyncio.get_running_loop()
        now = self._clock()

        def run() -> T:
            result = operation(now)
            if self._store.changed:  # waiters wake up even if the caller was cancelled
                loop.call_soon_threadsafe(self._notify)
            return result

        return self._executor.submit(run)

    def _release(self, now: float, claimed: Future[list[Lease]]) -> None:
        """Give back what a cancelled claim leased; it ran before, in the same thread."""
        if claimed.exception() is None:
            self._store.release(now, claimed.result())

    def _notify(self) -> None:
        self._changed.set()
        self._changed = asyncio.Event()


def _check_ttl(ttl: float) -> None:
    if ttl <= 0:
        raise ValueError(f"ttl must be positive, got {ttl}")
