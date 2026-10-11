from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pernix.request import Request

__all__ = ["MemoryFrontier"]


class MemoryFrontier:
    """An in-memory FIFO Frontier with exact-match deduplication.

    Every Request handed out by `get()` must be reported with `done()` after the
    Requests it produced have been added, so `join()` returns only once the Frontier is
    empty and nothing is in flight.
    """

    def __init__(self) -> None:
        """Create an empty Frontier."""
        self._queue: asyncio.Queue[Request] = asyncio.Queue()
        self._seen: set[tuple[str, str, bytes | None]] = set()

    def add(self, request: Request) -> bool:
        """Enqueue a Request unless an identical one was added before.

        Args:
            request: The Request to enqueue.

        Returns:
            `True` if it was enqueued, `False` if it was a duplicate.
        """
        key = (request.method, request.url, request.body)
        if key in self._seen:
            return False
        self._seen.add(key)
        self._queue.put_nowait(request)
        return True

    async def get(self) -> Request:
        """Wait for the next Request.

        Returns:
            The oldest pending Request.

        Raises:
            asyncio.QueueShutDown: The Frontier was shut down.
        """
        return await self._queue.get()

    def done(self) -> None:
        """Mark one Request returned by `get()` as fully processed."""
        self._queue.task_done()

    async def join(self) -> None:
        """Wait until every added Request has been marked done."""
        await self._queue.join()

    def shutdown(self) -> None:
        """Wake every waiting `get()` with `asyncio.QueueShutDown`."""
        self._queue.shutdown()
