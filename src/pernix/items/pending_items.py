from __future__ import annotations

import asyncio

__all__ = ["PendingItems"]


class PendingItems:
    """The Items of one attempt at a Request that are not processed yet.

    An Item is processed once it is stored, dropped, or failed and passed through
    the failure Pipelines. The Item processing counts each Item in as it takes it,
    and out once it is processed; the Engine settles the Lease on the Request once
    none is left.
    """

    def __init__(self) -> None:
        """Start with no Item pending."""
        self._count = 0
        self._none_left = asyncio.Event()
        self._none_left.set()

    @property
    def count(self) -> int:
        """How many Items are not processed yet."""
        return self._count

    def add(self) -> None:
        """Count in an Item taken for processing."""
        self._count += 1
        self._none_left.clear()

    def processed(self) -> None:
        """Count out an Item that is processed."""
        self._count -= 1
        if not self._count:
            self._none_left.set()

    async def wait(self) -> None:
        """Wait until every Item counted in is processed."""
        await self._none_left.wait()
