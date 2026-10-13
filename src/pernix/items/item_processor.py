from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.errors import DropItem, FeedError
from pernix.events import ItemDropped, ItemError, ItemScraped
from pernix.items.item_adapter import is_item
from pernix.items.pipeline import FailedItem
from pernix.items.pipeline_stage import PipelineStage

if TYPE_CHECKING:
    import random
    from collections.abc import Sequence

    from pernix.events import EventBus
    from pernix.feeds import Feed
    from pernix.items.pipeline import ItemContext
    from pernix.stats import Stats

__all__ = ["ItemProcessor"]

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Entry:
    item: Any
    ctx: ItemContext


@dataclass(slots=True)
class _Batch:
    entries: list[_Entry] = field(default_factory=list[_Entry])
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    timer: asyncio.Task[None] | None = None


class ItemProcessor:
    """Takes the Items of a Run through the Pipelines and into the Feeds, apart from the Callbacks.

    `put()` queues an Item, and waits while the queue is full, which suspends the
    Callback producing it. Workers take Items from the queue and pass each through
    the Pipelines that accept it, in order, then write it to every Feed that accepts
    it and emit `ItemScraped`. An Item a Pipeline drops is emitted as `ItemDropped`,
    and one a Pipeline fails on as `ItemError`; either way it goes no further. A
    batch Pipeline collects Items and processes them when its batch is full, inside
    the worker that filled it, when its timeout passes, and in `join()`; the batch
    then continues on its way.

    Each Item a Pipeline failed on then goes, as a `FailedItem`, through the failure
    Pipelines in order, each passing it on to the next or stopping it with `None`. A
    failure Pipeline that fails loses the failed Item: it is logged and counted as
    `items.failures_lost`.

    A `FeedError`, from a Feed or from any Pipeline, is emitted as `ItemError` and
    fails the task that raised it, so the Run fails rather than lose Items silently.
    """

    def __init__(
        self,
        pipelines: Sequence[object],
        feeds: Sequence[Feed],
        events: EventBus,
        stats: Stats,
        *,
        failed_pipelines: Sequence[object] = (),
        queue_size: int = 1000,
        concurrency: int = 1,
        timeout: float = 0.0,
        delay_scale: float = 1.0,
        rng: random.Random | None = None,
    ) -> None:
        """Configure the processing.

        Args:
            pipelines: The Pipelines, in order.
            feeds: The Feeds every Item that passes the Pipelines may go to.
            events: Where Item Events are emitted.
            stats: Counts Pipeline retries as `items.retried`, and failed Items the
                failure Pipelines lost as `items.failures_lost`.
            failed_pipelines: The failure Pipelines, in order.
            queue_size: Items waiting at most before `put()` waits.
            concurrency: Items processed at once.
            timeout: Seconds a Pipeline call may take unless the Pipeline sets its
                own; 0 for no limit.
            delay_scale: Multiplies every Pipeline retry delay.
            rng: The source of backoff jitter.

        Raises:
            pernix.errors.ComponentError: A Pipeline cannot process Items.
            ValueError: `queue_size` or `concurrency` is less than 1.
        """
        if queue_size < 1 or concurrency < 1:
            raise ValueError(f"queue_size and concurrency must be at least 1, got {queue_size} and {concurrency}")

        def stage(pipeline: object, *, batches: bool) -> PipelineStage:
            return PipelineStage(
                pipeline,
                timeout=timeout,
                batches=batches,
                delay_scale=delay_scale,
                on_retry=lambda: stats.inc("items.retried"),
                rng=rng,
            )

        self._stages = [stage(pipeline, batches=True) for pipeline in pipelines]
        self._failure_stages = [stage(pipeline, batches=False) for pipeline in failed_pipelines]
        self._batches = {index: _Batch() for index, stage in enumerate(self._stages) if stage.batch_size}
        self._feeds = tuple(feeds)
        self._events = events
        self._stats = stats
        self._queue: asyncio.Queue[_Entry] = asyncio.Queue(queue_size)
        self._concurrency = concurrency
        self._tasks: asyncio.TaskGroup | None = None

    async def put(self, item: Any, ctx: ItemContext) -> None:
        """Queue an Item, waiting while the queue is full.

        Args:
            item: The Item.
            ctx: Where it came from.
        """
        await self._queue.put(_Entry(item, ctx))

    def start(self, tasks: asyncio.TaskGroup) -> None:
        """Start the workers, and later the batch timers, in a task group.

        A Feed failure fails a task of the group, which cancels the others.

        Args:
            tasks: The task group of the Run.
        """
        self._tasks = tasks
        for _ in range(self._concurrency):
            tasks.create_task(self._work())

    async def join(self) -> None:
        """Process every queued Item and every pending batch, then stop the workers."""
        await self._queue.join()
        for index in sorted(self._batches):  # a batch may feed the batches of later Pipelines
            await self._flush(index)
        for batch in self._batches.values():
            if batch.timer is not None:
                batch.timer.cancel()
        self._queue.shutdown()

    async def _work(self) -> None:
        while True:
            try:
                entry = await self._queue.get()
            except asyncio.QueueShutDown:
                return
            try:
                await self._advance(entry, 0)
            finally:
                self._queue.task_done()

    async def _advance(self, entry: _Entry, start: int) -> None:
        """Take an Item through the Pipelines from `start` on, then deliver it."""
        for index in range(start, len(self._stages)):
            stage = self._stages[index]
            try:
                if not stage.wants(entry.item):
                    continue
                if index in self._batches:
                    await self._batch(index, entry)
                    return
                result = await stage.process(entry.item, entry.ctx)
                if result is not None and not is_item(result):
                    raise TypeError(f"process() returned {result!r}, which is not an Item")
            except DropItem as exc:
                await self._drop(entry, exc.reason or f"dropped by {stage.name}")
                return
            except FeedError as exc:
                await self._events.emit(ItemError(entry.item, entry.ctx.response, exc))
                raise
            except Exception as exc:
                await self._fail(entry, stage, exc)
                return
            if result is None:
                await self._drop(entry, f"{stage.name} returned None")
                return
            entry.item = result
        await self._deliver(entry)

    async def _batch(self, index: int, entry: _Entry) -> None:
        batch = self._batches[index]
        batch.entries.append(entry)
        if len(batch.entries) >= (self._stages[index].batch_size or 1):
            await self._flush(index)
        elif len(batch.entries) == 1 and self._tasks is not None:
            batch.timer = self._tasks.create_task(self._flush_later(index))

    async def _flush_later(self, index: int) -> None:
        await asyncio.sleep(self._stages[index].batch_timeout)
        await self._flush(index)

    async def _flush(self, index: int) -> None:
        """Process the pending Items of a batch Pipeline, a batch at a time, and pass them on."""
        stage, batch = self._stages[index], self._batches[index]
        async with batch.lock:
            if batch.timer is not None and batch.timer is not asyncio.current_task():
                batch.timer.cancel()
            batch.timer = None
            size = stage.batch_size or 1
            while batch.entries:
                entries, batch.entries = batch.entries[:size], batch.entries[size:]
                try:
                    await stage.process_batch([entry.item for entry in entries], [entry.ctx for entry in entries])
                except DropItem as exc:
                    for entry in entries:
                        await self._drop(entry, exc.reason or f"dropped by {stage.name}")
                    continue
                except FeedError as exc:
                    for entry in entries:
                        await self._events.emit(ItemError(entry.item, entry.ctx.response, exc))
                    raise
                except Exception as exc:
                    for entry in entries:
                        await self._fail(entry, stage, exc)
                    continue
                for entry in entries:
                    await self._advance(entry, index + 1)

    async def _deliver(self, entry: _Entry) -> None:
        for feed in self._feeds:
            if not feed.wants(entry.item):
                continue
            try:
                feed.write(entry.item)
            except FeedError as exc:
                logger.error("%s; the Run stops", exc)
                await self._events.emit(ItemError(entry.item, entry.ctx.response, exc))
                raise
        await self._events.emit(ItemScraped(entry.item, entry.ctx.response))

    async def _drop(self, entry: _Entry, reason: str) -> None:
        logger.debug("Dropped an Item from %r: %s", entry.ctx.request, reason)
        await self._events.emit(ItemDropped(entry.item, entry.ctx.response, reason))

    async def _fail(self, entry: _Entry, stage: PipelineStage, error: Exception) -> None:
        logger.error("Pipeline %s failed on an Item from %r", stage.name, entry.ctx.request, exc_info=error)
        await self._events.emit(ItemError(entry.item, entry.ctx.response, error))
        await self._record_failure(FailedItem(entry.item, stage.name, error), entry.ctx)

    async def _record_failure(self, failed: object, ctx: ItemContext) -> None:
        """Pass a failed Item through the failure Pipelines."""
        for stage in self._failure_stages:
            try:
                if not stage.wants(failed):
                    continue
                result = await stage.process(failed, ctx)
            except FeedError:
                raise
            except DropItem:
                return
            except Exception as exc:
                logger.error("Failure Pipeline %s lost a failed Item from %r", stage.name, ctx.request, exc_info=exc)
                self._stats.inc("items.failures_lost")
                return
            if result is None:
                return
            failed = result
