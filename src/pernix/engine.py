from __future__ import annotations

import asyncio
import logging
import os
import socket
from contextlib import aclosing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.callbacks import iterate_and_close
from pernix.events import RequestDropped, RequestScheduled, SpiderIdle
from pernix.items import PendingItems
from pernix.logs import request_context
from pernix.pausing import Pausing
from pernix.request_processor import Again, Dead, Done, PutItem, RequestProcessor
from pernix.retry_policy import DefaultRetryPolicy
from pernix.settings import DomainSettings, RetrySettings
from pernix.throttle import Throttle

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from pernix.events import EventBus
    from pernix.frontiers import Frontier, Lease
    from pernix.middlewares import Download, HandleResponse
    from pernix.request import Request
    from pernix.request_processor import Settlement
    from pernix.retry_policy import RetryPolicy
    from pernix.sending import Sender
    from pernix.spider import Spider
    from pernix.stats import Stats

__all__ = ["Engine", "PutItem"]

logger = logging.getLogger(__name__)


@dataclass(eq=False, slots=True)
class _Attempted:
    """A Request whose attempt ended, to settle once the Items of the attempt are processed.

    Attributes:
        lease: The Lease on the Request.
        settlement: How to settle it.
        items: The Items of the attempt.
        renewal: Renews the Lease until it is settled.
    """

    lease: Lease
    settlement: Settlement
    items: PendingItems
    renewal: asyncio.Task[None]


class Engine:
    """Runs a fixed number of workers that claim Requests from the Frontier and process them.

    With `browser_concurrency` workers of their own for the Requests that go to the
    browser Fetcher, the other workers claim only the Requests that do not, so those
    never wait for the browser, nor the browser for them; both kinds still share
    their domains' slots in the Throttle.

    Each worker claims one Request at a time under a Lease, skipping the slots the
    Throttle blocks, renews the Lease while it works, and makes one attempt at the
    Request with a `RequestProcessor`: fetching, retry decision, Callback or
    errback. The Lease is then settled as the attempt says, acknowledged, returned
    for another attempt, or kept as a Dead Request, once the Items the Callback and
    the errback yielded are processed: stored, dropped, or failed and passed through
    the failure Pipelines. Meanwhile the worker goes on to the next Request, and the
    Lease is still renewed. Each Request waiting so has an Item queued, in process,
    or in a batch, so the Item queue bounds them. Because a Request stays leased
    until its Callback's outputs have been added, the Requests its Callback adds are
    always in the Frontier before it leaves. If the Run is cancelled, Requests in
    flight, and those waiting for their Items, are returned to the Frontier without
    counting an attempt.

    When no attempt is in progress, and nothing else can happen before the Requests
    waiting for their Items are settled, as nothing in the Frontier is ready or
    delayed, or a Drain or a Pause stops the claiming, the Items are flushed
    (`flush_items`), so that those in a batch do not wait for it to fill or time out.

    The Run is idle once `start()` is exhausted, nothing is ready, delayed, or leased,
    and it is not paused. Then `SpiderIdle` is emitted and `Spider.on_idle()` runs; the
    Run ends when it adds no Request, once it is not paused. Every Request passes the
    `RequestScheduled` gate before entering the Frontier; a vetoed or duplicate
    Request is emitted as `RequestDropped`.

    `drain()` stops the claiming and the scheduling of the Requests `start()` and
    `on_idle()` yield; the Requests in flight are finished, with their Items, and the
    Requests their Callbacks yield still enter the Frontier. A Pause, which its
    `Pausing` carries out, stops the same until the Run is resumed; the Request
    `start()` or `on_idle()` is producing meanwhile still enters the Frontier.

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
        flush_items: Callable[[], None] | None = None,
        browser_concurrency: int = 0,
        lease_ttl: float = 60.0,
        retry_policy: RetryPolicy | None = None,
        throttle: Throttle | None = None,
        worker_id: str | None = None,
        sender: Sender | None = None,
        callback_timeout: float | None = None,
        stats: Stats | None = None,
        pausing: Pausing | None = None,
    ) -> None:
        """Wire the Engine to its collaborators.

        Args:
            spider: The Spider whose Requests and Callbacks to run.
            frontier: Where pending Requests are stored.
            download: Fetches a Request through the download Middleware and a Fetcher.
            handle: Runs the spider Middleware and the Callback for a Response,
                yielding the outputs; it ends in `pernix.callbacks.run_callbacks()`.
            put_item: Takes every Item the Callbacks and errbacks produce, with
                where it came from, and counts it in the `PendingItems` of its
                attempt until it is processed.
            events: Where Request and Response Events are emitted.
            concurrency: The number of workers, each processing one Request at a
                time, for the Requests that do not go to the browser Fetcher, or
                for every Request without `browser_concurrency`.
            flush_items: Has the Items taken so far processed, those in a batch
                included, without waiting for the batch to fill or time out; `None`
                when Items never wait in batches.
            browser_concurrency: The number of workers more, for the Requests that
                go to the browser Fetcher, as `pernix.fetchers.is_rendered()` tells;
                0 to have the `concurrency` workers process them too.
            lease_ttl: Seconds a claimed Request stays leased without renewal; Leases
                are renewed every third of it.
            retry_policy: Decides which attempts are retried; defaults to
                `DefaultRetryPolicy` with the default `RetrySettings`.
            throttle: Paces fetches per slot; defaults to the default
                `DomainSettings`.
            worker_id: Identifies this Worker to the Frontier; defaults to the host
                name and process id.
            sender: Sends each Request, under a Session leased from the Session
                Pool or as it is; `IndependentSending()` when `None`, for Requests
                that are independent and go through no proxy.
            callback_timeout: Seconds the spider chain may take to produce the
                outputs of a Response, and an errback its own, before it fails with
                `CallbackTimeout`; `None` for no limit.
            stats: Where, with a limit, the timeouts are counted.
            pausing: Pauses and resumes the Run, and emits its Events; by default,
                one of its own, which nothing pauses.

        Raises:
            ValueError: `concurrency` is less than 1, `browser_concurrency` is
                negative, or `lease_ttl` or `callback_timeout` is not positive.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        if browser_concurrency < 0:
            raise ValueError(f"browser_concurrency must not be negative, got {browser_concurrency}")
        if lease_ttl <= 0:
            raise ValueError(f"lease_ttl must be positive, got {lease_ttl}")
        self._spider = spider
        self._frontier = frontier
        self._events = events
        self._kinds: tuple[bool | None, ...] = (
            (False,) * concurrency + (True,) * browser_concurrency if browser_concurrency else (None,) * concurrency
        )
        """What each worker claims: rendered Requests, the others, or any (`None`)."""
        self._lease_ttl = lease_ttl
        self._throttle = throttle or Throttle(DomainSettings())
        self._worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._draining = asyncio.Event()
        self._pausing = pausing or Pausing(spider, events)
        self._flush_items = flush_items or (lambda: None)
        self._attempts = 0
        """The attempts in progress."""
        self._waiting = 0
        """The Requests whose attempts ended, until they are settled."""
        self._processor = RequestProcessor(
            spider,
            download=download,
            handle=handle,
            schedule=self._schedule,
            put_item=put_item,
            events=events,
            retry_policy=retry_policy or DefaultRetryPolicy(RetrySettings()),
            throttle=self._throttle,
            sender=sender,
            callback_timeout=callback_timeout,
            stats=stats,
        )

    async def run(self) -> None:
        """Run until the Run is idle and `Spider.on_idle()` adds no more Requests, or until drained.

        The Pausing begins with it, and ends once it is done, or drained.
        """
        self._pausing.begin()
        try:
            await self._run()
        finally:
            self._pausing.end()

    async def _run(self) -> None:
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(self._pausing.announce())
            workers = [tasks.create_task(self._work(rendered)) for rendered in self._kinds]
            feeding = tasks.create_task(self._feed())
            draining = tasks.create_task(self._draining.wait())
            await asyncio.wait([feeding, draining], return_when=asyncio.FIRST_COMPLETED)
            self._pausing.end()  # lets the announcing return once the changes are emitted
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

        `run()` returns once the Requests in flight are finished. It ends the
        Pausing, so a paused Run is drained without being resumed. Calling it again
        does nothing.
        """
        self._draining.set()
        self._pausing.end()

    async def _feed(self) -> None:
        """Schedule the Requests of `start()`, then those of `on_idle()` whenever the Run is idle, until it adds none.

        Scheduling into the Frontier need not suspend, so the Drain is checked before
        each step rather than left to cancelling this task. A paused Run ends only
        once it is resumed.
        """
        await self._schedule_all(self._spider.start(), "start()")
        while not self._draining.is_set():
            await self._until_idle()
            await self._events.emit(SpiderIdle(self._spider))
            if self._draining.is_set() or not await self._schedule_all(self._spider.on_idle(), "on_idle()"):
                break
        await self._pausing.released()

    async def _schedule_all(self, requests: AsyncIterator[Request], source: str) -> int:
        """Schedule the Requests a Spider method yields, until a Drain; return how many were added."""
        added = 0
        try:
            async with aclosing(iterate_and_close(requests)) as pending:
                while not self._draining.is_set():
                    if self._pausing.held:
                        await self._pausing.released()
                        continue
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
        """Wait until nothing is ready, delayed, or leased while the Run is not paused."""
        while True:
            await self._pausing.released()
            idle = not (await self._frontier.counts()).unfinished
            if idle and not self._pausing.held:  # paused while the Frontier counted
                return
            if not idle:
                await self._frontier.wait()

    async def _work(self, rendered: bool | None) -> None:
        """Process Requests of a kind, one at a time: rendered ones, the others, or any (`None`).

        A Request whose Items are still being processed once its attempt ends waits
        for them in a task of the worker's, which goes on to the next; on a Drain, the
        worker returns once they are settled.
        """
        handed_over: set[_Attempted] = set()  # to tasks of `waiting` that have not started yet
        try:
            async with asyncio.TaskGroup() as waiting:
                while not self._draining.is_set():
                    if self._pausing.held:
                        await self._flush_if_stuck()
                        await self._pausing.released()
                        continue
                    lease = await self._claim(rendered) or await self._wait_for_work(rendered)
                    if lease is None:
                        continue
                    with request_context(lease.id):  # records logged for the Request, its Callback's included
                        attempted = await self._process(lease)
                        if attempted.items.count:
                            handed_over.add(attempted)
                            waiting.create_task(self._settle_once_processed(attempted, handed_over))
                        else:
                            await self._settle_once_processed(attempted, handed_over)
                    await self._flush_if_stuck()
                await self._flush_if_stuck()
        finally:
            while handed_over:  # a failure or a cancellation stopped their tasks before they started
                attempted = handed_over.pop()
                await self._stop_waiting(attempted)
                await self._return(attempted.lease)

    async def _claim(self, rendered: bool | None) -> Lease | None:
        leases = await self._frontier.claim(
            self._worker_id, 1, self._lease_ttl, skip_slots=self._throttle.blocked(), rendered=rendered
        )
        return leases[0] if leases else None

    async def _wait_for_work(self, rendered: bool | None) -> Lease | None:
        """Wait until the Frontier changes, a blocked slot may start again, or a Drain begins; return a Lease found meanwhile.

        Paused or drained, it returns at once, for the worker to wait for that instead.
        """
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
            if not self._draining.is_set() and not self._pausing.held:
                lease = await self._claim(rendered)
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

    async def _process(self, lease: Lease) -> _Attempted:
        """Make an attempt at a Request, renewing its Lease, which goes on until it is settled."""
        items = PendingItems()
        renewal = asyncio.create_task(self._keep_leased(lease))
        self._attempts += 1
        try:
            settlement = await self._processor.attempt(lease, items)
        except BaseException as exc:
            renewal.cancel()
            await asyncio.wait([renewal])
            if isinstance(exc, asyncio.CancelledError):
                await self._return(lease)
            raise
        finally:
            self._attempts -= 1
        self._waiting += 1
        return _Attempted(lease, settlement, items, renewal)

    async def _settle_once_processed(self, attempted: _Attempted, handed_over: set[_Attempted]) -> None:
        """Settle a Lease once the Items of its attempt are processed, taking it from those handed over."""
        handed_over.discard(attempted)
        lease = attempted.lease
        try:
            await attempted.items.wait()
            attempted.renewal.cancel()  # before settling, so a renewal never races the settlement
            settled = await self._settle(lease, attempted.settlement)
        except asyncio.CancelledError:
            # Until settled, the Request goes back; once settled, this write is stale and changes nothing.
            await self._return(lease)
            raise
        finally:
            await self._stop_waiting(attempted)
        if not settled:
            logger.warning("The Lease on %r expired before it was settled; it may be processed again", lease.request)

    async def _stop_waiting(self, attempted: _Attempted) -> None:
        """Stop renewing the Lease of a Request that waits no more, settled or given back."""
        attempted.renewal.cancel()
        await asyncio.wait([attempted.renewal])
        self._waiting -= 1

    async def _flush_if_stuck(self) -> None:
        """Flush the Items when nothing else can happen before the Requests waiting for them are settled.

        That is when no attempt is in progress, and a Drain or a Pause stops the
        claiming, or nothing else in the Frontier is ready, delayed, or leased.
        """
        if self._attempts or not self._waiting:
            return
        if not self._draining.is_set() and not self._pausing.held:
            counts = await self._frontier.counts()
            if counts.ready or counts.delayed or counts.leased > self._waiting:
                return
        self._flush_items()

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
            case Again(delay, reason, count_attempt, budget, request):
                return await self._frontier.nack(
                    lease, delay=delay, count_attempt=count_attempt, reason=reason, budget=budget, request=request
                )
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
