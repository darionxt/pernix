from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
from functools import partial
from typing import TYPE_CHECKING

from pernix.errors import SerializationError
from pernix.fetchers.fetcher_dispatch import is_rendered
from pernix.fingerprint import request_fingerprint
from pernix.frontiers.frontier import FrontierCounts, host_slot
from pernix.frontiers.sqlite_store import FrontierStore, RequestRow, open_read_only, read_counts, read_dead_page
from pernix.request_codec import decode_request, encode_request, method_names, stored_callbacks

if TYPE_CHECKING:
    import sqlite3
    from collections.abc import AsyncIterator, Callable, Collection, Generator, Iterator, Mapping, Sequence
    from concurrent.futures import Future

    from pernix.frontiers.frontier import Lease
    from pernix.request import Request
    from pernix.spider import Spider

__all__ = ["DeadRequest", "FrontierReader", "SQLiteFrontier"]

logger = logging.getLogger(__name__)

_DEAD_PAGE = 500
"""The Dead Requests `SQLiteFrontier.dead_requests()` reads in one transaction."""


@dataclass(frozen=True, slots=True)
class DeadRequest:
    """A Dead Request, as `SQLiteFrontier.dead_requests()` describes it, without restoring it.

    Attributes:
        id: The Frontier's id of the Request, as in `Lease.id`.
        url: Its URL.
        reason: Why it failed for good: the type of the error, a colon, and its
            message, such as `RetriesExhausted: HTTP 503`.
        attempts: Its attempts, the last of which failed for good, as
            `Lease.attempt` counts them.
        spent: The retries it had under each retry budget, by name.
        callback: The name of the Spider method its Callback is; `None` for one
            without, which the Spider's `parse` handles, and when the Request as
            stored cannot be read.
        errback: The name of its errback, if it has one.
        rendered: Whether it goes to the browser Fetcher.
    """

    id: str
    url: str
    reason: str
    attempts: int
    spent: Mapping[str, int] = field(default_factory=dict[str, int])
    callback: str | None = None
    errback: str | None = None
    rendered: bool = False

    @property
    def error(self) -> str:
        """The type of the error it failed with: its reason up to the first colon, or all of it without one."""
        kind, colon, _ = self.reason.partition(": ")
        return kind if colon else self.reason


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
    since the calling task last used it, and when the next deadline falls due, so
    one process at a time should use a database. Dead Requests can be inspected in
    the table `requests`, with their `url` and `reason`, or with `dead_requests()`,
    and revived with `revive()`.

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
        self._store = FrontierStore(os.fspath(path), partial(decode_request, spider=spider), clock)
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pernix-frontier")
        self._changed = asyncio.Event()
        self._changes = 0
        """The operations that changed something, counted in the thread as they end."""
        self._notified = 0
        """The changes the waiters were woken for, counted on the event loop."""
        # One per Frontier, so that the contexts of tasks keep no Frontier alive.
        self._seen: ContextVar[int] = ContextVar("pernix_frontier_seen")
        """The changes the current task had seen when it last used the Frontier."""
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
        return await self._call(lambda: self._store.add(rows))

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

        A Request that cannot be restored, as `decode_request()` tells, such as one
        whose Callback is no longer a method of the Spider, is kept as a Dead Request
        instead, with the error as its reason, and logged as an error.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        _check_ttl(ttl)
        skipped = frozenset(skip_slots)
        claimed = self._submit(lambda: self._store.claim(worker_id, limit, ttl, skipped, rendered))
        try:
            leases, lost = self._seeing(await asyncio.shield(asyncio.wrap_future(claimed)))
        except asyncio.CancelledError:
            if not self._closed:  # the claim goes on in the thread; what it leases goes back after it
                self._submit(lambda: self._release(claimed))
            raise
        for error in lost:
            logger.error("Kept a Request that cannot be restored as a Dead Request: %s", error)
        return leases

    async def release_leases(self) -> int:
        """Make every leased Request ready again, counting an attempt, as if its Lease had expired.

        A process that alone uses the database, such as by holding the lock of a job
        directory, calls it as it starts, so that the Requests the Workers of an
        earlier process held are fetched again at once, rather than once their
        Leases expire. A Request that ended its process thus cannot do so forever.

        Returns:
            How many Requests were leased.
        """
        return await self._call(self._store.release_leases)

    async def missing_methods(self) -> list[str]:
        """Return the methods the Requests not done or dead refer to that the Spider no longer has.

        Those of Requests stored by a Pernix before schema version 2 are not known.

        Returns:
            Their names, in order.
        """
        names = await self._call(self._store.method_names)
        return sorted(name for name in names if not inspect.ismethod(getattr(self._spider, name, None)))

    async def dead_requests(self) -> AsyncIterator[DeadRequest]:
        """Yield the Dead Requests, oldest first, reading a few hundred at a time.

        They are described without being restored, so the Spider need not have
        their methods.

        Yields:
            Each Dead Request.
        """
        after = 0
        while page := await self._call(partial(self._dead_page, after)):
            for dead in page:
                yield dead
            after = int(page[-1].id)

    async def revive(self, ids: Collection[str]) -> int:
        """Make Dead Requests ready again, with no attempt counted and no retry under any budget.

        The next attempt at each is thus its first. They queue behind the Requests
        of their priority that are ready, in the order they were added, and keep
        their reasons until they are settled again.

        Args:
            ids: Their ids, as `DeadRequest.id`; those of Requests that are not dead
                are skipped.

        Returns:
            How many were revived.

        Raises:
            ValueError: An id is not one this Frontier gives.
        """
        rows = sorted({int(request_id) for request_id in ids})
        return await self._call(lambda: self._store.revive(rows))

    async def renew(self, lease: Lease, ttl: float) -> Lease | None:
        """Extend a Lease; see `Frontier.renew()`."""
        _check_ttl(ttl)
        expires_at = await self._call(lambda: self._store.renew(lease, ttl))
        return None if expires_at is None else replace(lease, expires_at=expires_at)

    async def ack(self, lease: Lease) -> bool:
        """Mark a leased Request as done, keeping its Fingerprint; see `Frontier.ack()`."""
        return await self._call(lambda: self._store.ack(lease))

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
            lambda: self._store.nack(
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
        return await self._call(lambda: self._store.dead(lease, reason))

    async def counts(self) -> FrontierCounts:
        """Count the Requests in each state, done ones included; see `Frontier.counts()`."""
        return await self._call(self._store.counts)

    async def wait(self) -> None:
        """Wait for a change made through this Frontier, or for the next deadline; see `Frontier.wait()`.

        It returns at once if a change was made since the calling task last used
        this Frontier: the task may have checked what it waits for then, and the
        change came while that check was waiting for the database.
        """
        changed = self._changed  # a change the operation below makes sets this one
        try:
            if self._seen.get(self._notified) < self._notified:
                return
            due = await self._call(self._store.next_due)
            try:
                async with asyncio.timeout(None if due is None else max(0.0, due - self._clock())):
                    await changed.wait()
            except TimeoutError:
                pass
        finally:
            self._seen.set(self._notified)  # the caller checks again once it returns

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

    def _dead_page(self, after: int) -> list[DeadRequest]:
        """Read the Dead Requests stored after the one of the id `after`, in the Frontier's thread."""
        return [_dead_request(*row) for row in self._store.dead_page(after, _DEAD_PAGE)]

    def _key(self, request: Request) -> str:
        return request.fingerprint if request.fingerprint is not None else self._fingerprint(request)

    def _row(self, request: Request, fingerprint: str | None, slot: str) -> RequestRow:
        """Prepare a Request for the database, on the event loop, where its Fingerprint and slot were computed."""
        data = encode_request(request, self._spider)
        return RequestRow(
            fingerprint, slot, is_rendered(request), request.priority, request.url, data, method_names(request)
        )

    async def _call[T](self, operation: Callable[[], T]) -> T:
        """Run an operation of the store; it completes even if the caller is cancelled."""
        return self._seeing(await asyncio.shield(asyncio.wrap_future(self._submit(operation))))

    def _submit[T](self, operation: Callable[[], T]) -> Future[tuple[T, int]]:
        """Queue an operation of the store, waking the waiters once it changed something.

        Operations run one at a time, in the order they are submitted, in the
        Frontier's thread and its own context, so the Requests restored there take
        nothing from the caller's, such as the page a Callback handles. Each gives
        its result with the number of changes made by then.
        """
        if self._closed:
            raise RuntimeError("the Frontier is closed")
        loop = asyncio.get_running_loop()

        def run() -> tuple[T, int]:
            result = operation()
            if self._store.changed:  # waiters wake up even if the caller was cancelled
                self._changes += 1
                loop.call_soon_threadsafe(self._notify)
            return result, self._changes

        return self._executor.submit(run)

    def _seeing[T](self, outcome: tuple[T, int]) -> T:
        """Record, for the calling task, how many changes the result of an operation saw; return the result."""
        result, changes = outcome
        self._seen.set(changes)
        return result

    def _release(self, claimed: Future[tuple[tuple[list[Lease], list[str]], int]]) -> None:
        """Give back what a cancelled claim leased; it ran before, in the same thread."""
        if claimed.exception() is None:
            self._store.release(claimed.result()[0][0])

    def _notify(self) -> None:
        self._notified += 1
        self._changed.set()
        self._changed = asyncio.Event()


class FrontierReader:
    """The database of a `SQLiteFrontier`, read as it is, changing nothing, also while a Run writes it.

    Its counts and Dead Requests are read as `SQLiteFrontier.counts()` and
    `dead_requests()` read them, but without first making the delays that passed
    and the Leases that expired ready, nor migrating an older schema, nor creating
    anything. Each read opens the database anew, read-only, as
    `pernix.frontiers.sqlite_store.open_read_only()` tells by what it then finds, and
    closes it before what it read is handed over: the reader holds nothing between
    reads, so that it never holds back a Run's log, and a database read as one
    nothing changes is so read once only, which a Run resumed later may change. The
    counts are those of the moment they are read, and the Dead Requests are read a
    few hundred at a time, each page as it then is.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        """Check that the database can be read.

        Args:
            path: The database file.

        Raises:
            pernix.errors.SerializationError: The file is not a Frontier's
                database, or one of a schema newer than this Pernix reads, as each
                read raises too should the database become one.
        """
        self._path = os.fspath(path)
        with self._reading():
            pass

    def counts(self) -> FrontierCounts:
        """Count the Requests in each state, as they are stored: none for a database no Frontier made its schema in."""
        with self._reading() as connection:
            return FrontierCounts(0, 0, 0, 0, 0) if connection is None else read_counts(connection)

    def dead_requests(self) -> Iterator[DeadRequest]:
        """Yield the Dead Requests, oldest first, as `SQLiteFrontier.dead_requests()` describes them.

        Yields:
            Each Dead Request.
        """
        after = 0
        while True:
            with self._reading() as connection:
                page = [] if connection is None else read_dead_page(connection, after, _DEAD_PAGE)
            if not page:
                return
            for row in page:
                yield _dead_request(*row)
            after = page[-1][0]

    @contextmanager
    def _reading(self) -> Generator[sqlite3.Connection | None]:
        connection = open_read_only(self._path)
        try:
            yield connection
        finally:
            if connection is not None:
                connection.close()


def _dead_request(
    request_id: int, url: str, reason: str, attempts: int, spent: str, rendered: int, data: bytes
) -> DeadRequest:
    try:
        callback, errback = stored_callbacks(data)
    except SerializationError:
        callback = errback = None
    return DeadRequest(str(request_id), url, reason, attempts + 1, json.loads(spent), callback, errback, bool(rendered))


def _check_ttl(ttl: float) -> None:
    if ttl <= 0:
        raise ValueError(f"ttl must be positive, got {ttl}")
