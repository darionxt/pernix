from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Self

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable
    from types import TracebackType

    from pernix.run_result import RunResult
    from pernix.shutdown import StopMode

__all__ = ["ItemStream"]


class ItemStream:
    """The Items of a Run as they pass every Pipeline; iterating over them runs the Run.

    The Run starts with the first iteration and ends the iteration when it ends. If it
    fails, the Items already received are kept, and the error is raised once they are
    exhausted. The stream holds one Item at a time, so a slow consumer slows the Run
    down: the Item processing waits for it, the Item queue fills up, the Callbacks
    pause, and no new Requests are fetched.

    Leaving early force-stops the Run. Leaving an `async with` block, or calling
    `aclose()`, waits until the Run has stopped; a stream simply abandoned is
    stopped only once it is garbage-collected.

    Example:
        >>> async with Books.stream_items(keyword="dune") as items:
        ...     async for item in items:
        ...         print(item)
    """

    def __init__(
        self,
        crawl: Callable[[], Awaitable[RunResult]],
        stop: Callable[[StopMode], None],
        channel: asyncio.Queue[Any],
    ) -> None:
        """Stream the Items of a Run; `pernix.crawler.Crawler.stream_items()` creates it.

        Args:
            crawl: Runs the Run.
            stop: Stops the Run.
            channel: Where the Run puts its Items; its size bounds the Items held.
        """
        # Only the stream holds the generator, and nothing the Run holds refers back to
        # either: dropping the stream finalizes the generator, which stops the Run.
        self._items = _items(crawl, stop, channel)

    def __aiter__(self) -> Self:
        return self

    async def __anext__(self) -> Any:
        return await anext(self._items)

    async def aclose(self) -> None:
        """Force-stop the Run, if it is still going, and wait until it has stopped."""
        await self._items.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.aclose()


async def _items(
    crawl: Callable[[], Awaitable[RunResult]],
    stop: Callable[[StopMode], None],
    channel: asyncio.Queue[Any],
) -> AsyncGenerator[Any]:
    run = asyncio.create_task(_run(crawl, channel))
    try:
        while True:
            try:
                item = await channel.get()
            except asyncio.QueueShutDown:
                break
            yield item
    finally:
        if not run.done():  # left early
            stop("force")
        await run  # raises the error the Run failed with


async def _run(crawl: Callable[[], Awaitable[RunResult]], channel: asyncio.Queue[Any]) -> RunResult:
    try:
        return await crawl()
    finally:
        channel.shutdown()  # the Items already in the channel can still be taken
