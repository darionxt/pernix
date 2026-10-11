from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping
from contextlib import aclosing
from typing import TYPE_CHECKING, Any, cast

from pernix.request import Request

if TYPE_CHECKING:
    from pernix.fetchers import Fetcher
    from pernix.memory_frontier import MemoryFrontier
    from pernix.spider import Spider

__all__ = ["Engine", "ItemSink"]

logger = logging.getLogger(__name__)

type ItemSink = Callable[[Mapping[str, Any]], None]
"""Receives each Item a Callback produces."""


class Engine:
    """Runs a fixed number of workers that fetch Requests and run their Callbacks.

    The Run ends once `start()` is exhausted, the Frontier is empty, and nothing is in
    flight. Exceptions raised while fetching or inside a Callback are logged and do not
    stop the Run.
    """

    def __init__(
        self,
        spider: Spider,
        *,
        frontier: MemoryFrontier,
        fetcher: Fetcher,
        item_sink: ItemSink,
        concurrency: int,
    ) -> None:
        """Wire the Engine to its collaborators.

        Args:
            spider: The Spider whose Requests and Callbacks to run.
            frontier: Where pending Requests are stored.
            fetcher: Turns Requests into Responses.
            item_sink: Receives every Item the Callbacks produce.
            concurrency: The number of workers, which is also the maximum number of
                Requests processed at once.

        Raises:
            ValueError: `concurrency` is less than 1.
        """
        if concurrency < 1:
            raise ValueError(f"concurrency must be at least 1, got {concurrency}")
        self._spider = spider
        self._frontier = frontier
        self._fetcher = fetcher
        self._item_sink = item_sink
        self._concurrency = concurrency

    async def run(self) -> None:
        """Run until every Request has been fetched and its Callback has finished."""
        async with asyncio.TaskGroup() as tasks:
            for _ in range(self._concurrency):
                tasks.create_task(self._work())
            await self._add_start_requests()
            await self._frontier.join()
            self._frontier.shutdown()

    async def _add_start_requests(self) -> None:
        try:
            async with aclosing(_iterate(self._spider.start())) as requests:
                async for request in requests:
                    self._frontier.add(request)
        except Exception:
            logger.exception("start() of spider %r failed", self._spider.name)

    async def _work(self) -> None:
        while True:
            try:
                request = await self._frontier.get()
            except asyncio.QueueShutDown:
                return
            try:
                await self._process(request)
            finally:
                self._frontier.done()

    async def _process(self, request: Request) -> None:
        try:
            response = await self._fetcher.fetch(request)
        except Exception:
            logger.exception("Fetching %r failed", request)
            return

        callback = request.callback or self._spider.parse
        try:
            async with aclosing(_outputs(callback(response))) as outputs:
                async for output in outputs:
                    self._handle_output(output, request)
        except Exception:
            logger.exception("Callback %s failed for %r", _name(callback), response)

    def _handle_output(self, output: object, source: Request) -> None:
        match output:
            case None:
                pass
            case Request():
                self._frontier.add(output)
            case Mapping():
                self._item_sink(output)  # pyright: ignore[reportUnknownArgumentType]
            case _:
                logger.warning("Ignoring unsupported output %r from the Callback for %r", output, source)


async def _iterate[T](items: AsyncIterator[T]) -> AsyncGenerator[T]:
    """Iterate `items`, closing it promptly if it is an async generator."""
    if isinstance(items, AsyncGenerator):
        async with aclosing(cast("AsyncGenerator[T]", items)) as generator:
            async for item in generator:
                yield item
    else:
        async for item in items:
            yield item


async def _outputs(result: object) -> AsyncGenerator[object]:
    """Normalize a Callback's result: an async iterator, or an awaitable of a value or list."""
    if isinstance(result, AsyncIterator):
        async with aclosing(_iterate(cast("AsyncIterator[object]", result))) as outputs:
            async for output in outputs:
                yield output
        return
    value = await result if inspect.isawaitable(result) else result
    if isinstance(value, list):
        for output in cast("list[object]", value):
            yield output
    else:
        yield value


def _name(callback: Callable[..., Any]) -> str:
    return getattr(callback, "__qualname__", repr(callback))
