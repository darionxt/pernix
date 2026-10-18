"""The pages a browser Fetcher may have open at once, each taken by one fetch until its page is closed."""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.stats import Stats

__all__ = ["PageSlot", "PageSlots"]


class PageSlot:
    """A page that a fetch may have open, taken from `PageSlots` until released.

    The fetch that took it releases it once its page is closed, or hands it on to
    what keeps the page open longer, which releases it then.
    """

    def __init__(self, give_back: Callable[[], None]) -> None:
        """Hold a slot.

        Args:
            give_back: Returns the slot to its `PageSlots`.
        """
        self._give_back: Callable[[], None] | None = give_back

    @property
    def released(self) -> bool:
        """Whether the slot was released."""
        return self._give_back is None

    def release(self) -> None:
        """Give the slot back, for another fetch to open a page; releasing it again does nothing."""
        give_back, self._give_back = self._give_back, None
        if give_back is not None:
            give_back()


class PageSlots:
    """Lets at most `limit` pages be open at once, handing slots out in the order fetches ask for them.

    In `stats`, if given, `browser.pages_open` is the number of slots taken, and
    `browser.page_waits` and `browser.page_wait_seconds` count the fetches that
    waited for one, and how long.
    """

    def __init__(self, limit: int, *, stats: Stats | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        """Configure the slots.

        Args:
            limit: Pages open at once, at most; at least 1.
            stats: Where the slots taken and the waits are counted.
            clock: A monotonic clock, in seconds, timing the waits.

        Raises:
            ValueError: `limit` is less than 1.
        """
        if limit < 1:
            raise ValueError(f"limit must be at least 1, got {limit}")
        self._free = asyncio.Semaphore(limit)
        self._taken = 0
        self._stats = stats
        self._clock = clock

    @property
    def taken(self) -> int:
        """The slots taken and not released yet."""
        return self._taken

    async def take(self) -> PageSlot:
        """Wait for a slot and take it.

        Returns:
            The slot, to release once the page is closed.
        """
        waited = self._free.locked()
        started = self._clock()
        await self._free.acquire()
        if waited and self._stats is not None:
            self._stats.inc("browser.page_waits")
            self._stats.inc("browser.page_wait_seconds", self._clock() - started)
        self._taken += 1
        self._report()
        return PageSlot(self._give_back)

    def _give_back(self) -> None:
        self._taken -= 1
        self._report()
        self._free.release()

    def _report(self) -> None:
        if self._stats is not None:
            self._stats.set("browser.pages_open", self._taken)
