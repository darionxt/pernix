from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import aclosing
from typing import TYPE_CHECKING

from pernix.callbacks import iterate_and_close
from pernix.events import RequestDropped, RequestScheduled, SpiderIdle
from pernix.logs import request_context
from pernix.request_processor import Again, Dead, Done, PutItem, RequestProcessor
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import DomainSettings, RetrySettings
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix.events import EventBus
    from pernix.frontiers import Frontier, Lease
    from pernix.middlewares import Download, HandleResponse
    from pernix.request import Request
    from pernix.request_processor import Settlement
    from pernix.retry_policy import RetryPolicy
    from pernix.sessions.session_leasing import SessionLeasing
    from pernix.spider import Spider

__all__ = ["Engine", "PutItem"]

logger = logging.getLogger(__name__)


class Engine:
    """Runs a fixed number of workers that claim Requests from the Frontier and process them.

    Each worker claims one Request at a time under a Lease, skipping the slots the
    Throttle blocks, renews the Lease while it works, and makes one attempt at the
    Request with a `RequestProcessor`: fetching, retry decision, Callback or
    errback. The Lease is then settled as the attempt says: acknowledged, returned
    for another attempt, or kept as a Dead Request. Because a Request stays leased
    until its Callback's outputs have been added, the Requests its Callback adds are
    always in the Frontier before it leaves. If the Run is cancelled, Requests in
    flight are returned to the Frontier without counting an attempt.

    The Run is idle once `start()` is exhausted and nothing is ready, delayed, or
    leased. Then `SpiderIdle` is emitted and `Spider.on_idle()` runs; the Run ends when
    it adds no Request. Every Request passes the `RequestScheduled` gate before
    entering the Frontier; a vetoed or duplicate Request is emitted as
    `RequestDropped`.

    `drain()` stops the claiming and the scheduling of the Requests `start()` and
    `on_idle()` yield; the Requests in flight are finished, and the Requests their
    Callbacks yield still enter the Frontier.

    Requests constructed in the spider chain resolve relative URLs against the
    Response handled by the layer, or the Callback, that constructs them. While a
    Request is processed, its Frontier id is the `request_id` of the records logged.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        frontier: Frontier,
        download: Download,
        handle: HandleResponse,
        put_item: PutItem,
        events: EventBus,
        concurrency: int,
        lease_ttl: float = 60.0,
        retry_policy: RetryPolicy | None = None,
        throttle: Throttle | None = None,
        worker_id: str | None = None,
        sessions: SessionLeasing | None = None,
    ) -> None:
        """Wire the Engine to its collaborators.

        Args:
            spider: The Spider whose Requests and Callbacks to run.
            frontier: Where pending Requests are stored.
            download: Fetches a Request through the download Middleware and a Fetcher.
            handle: Runs the spider Middleware and the Callback for a Response,
                yielding the outputs; it ends in `pernix.callbacks.run_callbacks()`.
            put_item: Takes every Item the Callbacks and errbacks produce, with
                where it came from.
            events: Where Request and Response Events are emitted.
            concurrency: The number of workers, which is also the maximum number of
                Requests processed at once.
            lease_ttl: Seconds a claimed Request stays leased without renewal; Leases
                are renewed every third of it.
            retry_policy: Decides which attempts are retried; defaults to
                `DefaultRetryPolicy` with the default `RetrySettings`.
            throttle: Paces fetches per slot; defaults to the default
                `DomainSettings`.
            worker_id: Identifies this Worker to the Frontier; defaults to the host
                name and process id.
            sessions: Leases Sessions from the Session Pool; `None` without one,
                when Requests are independent.

        Raises:
            ValueError: `concurrency` is less than 1, or `lease_ttl` is not positive.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        if lease_ttl <= 0:
            raise ValueError(f"lease_ttl must be positive, got {lease_ttl}")
        self._spider = spider
        self._frontier = frontier
        self._events = events
        self._concurrency = concurrency
        self._lease_ttl = lease_ttl
        self._throttle = throttle or Throttle(DomainSettings())
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._draining = asyncio.Event()
        self._processor = RequestProcessor(
            spider,
            download=download,
            handle=handle,
            schedule=self._schedule,
            put_item=put_item,
            events=events,
            retry_policy=retry_policy or DefaultRetryPolicy(RetrySettings()),
            throttle=self._throttle,
            sessions=sessions,
        )

    async def run(self) -> None:
        """Run until the Run is idle and `Spider.on_idle()` adds no more Requests, or until drained."""
        async with asyncio.TaskGroup() as tasks:
            workers = [tasks.create_task(self._work()) for _ in range(self._concurrency)]
            feeding = tasks.create_task(self._feed())
            draining = tasks.create_task(self._draining.wait())
            await asyncio.wait([feeding, draining], return_when=asyncio.FIRST_COMPLETED)
            if feeding.done():
                feeding.result()  # a cancellation from start() or on_idle() is not an ending
            if self._draining.is_set():  # the feeding may have stopped for the Drain, with work in flight
                feeding.cancel()  # the workers finish the Requests in flight, then return
            else:
                draining.cancel()
                for worker in workers:  # all of them are waiting for work that will never come
                    worker.cancel()

    def drain(self) -> None:
        """Stop claiming Requests and scheduling those of `start()` and `on_idle()`.

        `run()` returns once the Requests in flight are finished. Calling it again
        does nothing.
        """
        self._draining.set()

    async def _feed(self) -> None:
        """Schedule the Requests of `start()`, then those of `on_idle()` whenever the Run is idle, until it adds none.

        Scheduling into the Frontier need not suspend, so the Drain is checked before
        each step rather than left to cancelling this task.
        """
        await self._schedule_all(self._spider.start(), "start()")
        while not self._draining.is_set():
            await self._until_idle()
            await self._events.emit(SpiderIdle(self._spider))
            if self._draining.is_set() or not await self._schedule_all(self._spider.on_idle(), "on_idle()"):
                return

    async def _schedule_all(self, requests: AsyncIterator[Request], source: str) -> int:
        """Schedule the Requests a Spider method yields, until a Drain; return how many were added."""
        added = 0
        try:
            async with aclosing(iterate_and_close(requests)) as pending:
                while not self._draining.is_set():
                    try:
                        request = await anext(pending)
                    except StopAsyncIteration:
                        break
                    if self._draining.is_set():  # requested while the producer ran
                        break
                    added += await self._schedule(request)
        except Exception:
            logger.exception("%s of spider %r failed", source, self._spider.name)
        return added

    async def _until_idle(self) -> None:
        while (await self._frontier.counts()).unfinished:
            await self._frontier.wait()

    async def _work(self) -> None:
        while not self._draining.is_set():
            lease = await self._claim() or await self._wait_for_work()
            if lease is not None:
                with request_context(lease.id):  # records logged for the Request, its Callback's included
                    await self._process(lease)

    async def _claim(self) -> Lease | None:
        leases = await self._frontier.claim(self._worker_id, 1, self._lease_ttl, skip_slots=self._throttle.blocked())
        return leases[0] if leases else None

    async def _wait_for_work(self) -> Lease | None:
        """Wait until the Frontier changes, a blocked slot may start again, or a Drain begins; return a Lease found meanwhile."""
        loop = asyncio.get_running_loop()
        # Started eagerly, the waits register for changes before the claim below, so a
        # change made between an empty claim and the wait cannot go unnoticed.
        waits = [
            asyncio.Task(self._frontier.wait(), loop=loop, eager_start=True),
            asyncio.Task(self._throttle.wait(), loop=loop, eager_start=True),
            asyncio.Task(self._draining.wait(), loop=loop, eager_start=True),
        ]
        lease: Lease | None = None
        try:
            if not self._draining.is_set():
                lease = await self._claim()
            if lease is None:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
            try:
                await asyncio.wait(waits)
            except asyncio.CancelledError:
                if lease is not None:  # claimed, but cancelled before it could be processed
                    await self._return(lease)
                raise
        return lease

    async def _process(self, lease: Lease) -> None:
        renewal = asyncio.create_task(self._keep_leased(lease))
        try:
            settlement = await self._processor.attempt(lease)
            renewal.cancel()  # before settling, so a renewal never races the settlement
            settled = await self._settle(lease, settlement)
        except asyncio.CancelledError:
            # Until settled, the Request goes back; once settled, this write is stale and changes nothing.
            await self._return(lease)
            raise
        finally:
            renewal.cancel()
            await asyncio.wait([renewal])
        if not settled:
            logger.warning("The Lease on %r expired before it was settled; it may be processed again", lease.request)

    async def _keep_leased(self, lease: Lease) -> None:
        """Renew a Lease until cancelled, or until it is lost."""
        while True:
            await asyncio.sleep(self._lease_ttl / 3)
            try:
                renewed = await self._frontier.renew(lease, self._lease_ttl)
            except Exception:
                logger.exception("Renewing the Lease on %r failed", lease.request)
                return
            if renewed is None:
                logger.warning("Lost the Lease on %r while processing it; it may be processed again", lease.request)
                return

    async def _return(self, lease: Lease) -> None:
        """Give an unfinished Request back to the Frontier, without counting the attempt."""
        try:
            await self._frontier.nack(lease, count_attempt=False, reason="cancelled")
        except Exception:
            logger.exception("Returning %r to the Frontier failed", lease.request)

    async def _settle(self, lease: Lease, settlement: Settlement) -> bool:
        match settlement:
            case Again(delay, reason, count_attempt):
                return await self._frontier.nack(lease, delay=delay, count_attempt=count_attempt, reason=reason)
            case Dead(reason):
                return await self._frontier.dead(lease, reason)
            case Done():
                return await self._frontier.ack(lease)

    async def _schedule(self, request: Request) -> bool:
        veto = await self._events.gate(RequestScheduled(request))
        if veto is not None:
            await self._events.emit(RequestDropped(request, veto.reason))
            return False
        [added] = await self._frontier.add([request])
        if not added:
            await self._events.emit(RequestDropped(request, "duplicate"))
        return added
