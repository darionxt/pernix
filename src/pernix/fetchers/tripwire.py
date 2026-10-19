"""A tripwire: once tripped, the tasks inside its scopes are cancelled, and raise an error of its choosing instead."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Sequence

__all__ = ["GRACE", "Tripwire", "first_tripped"]

GRACE = 1.0
"""Seconds an error that may come of what trips the wire waits for it to be tripped."""


class Tripwire:
    """Stops the work inside its scopes once something it guards against happens, as a page crashing.

    The work is cancelled, as `asyncio.timeout()` cancels it, and the scope raises
    the error it is given for the reason it was tripped with; a cancellation from
    elsewhere stays a cancellation. Patchright may tell of what happened first by
    failing the work, before the event that trips the wire: an error the scope is
    told may come of it waits a moment for the wire to be tripped, and becomes the
    scope's error if it is.

    Attributes:
        reason: Why it was tripped; `None` until then.
    """

    def __init__(self) -> None:
        """Create a tripwire that has not been tripped."""
        self.reason: str | None = None
        self._tripped = asyncio.Event()
        self._cancels: set[Callable[[], None]] = set()

    def trip(self, reason: str) -> None:
        """Trip the wire, cancelling the work inside its scopes; tripping it again does nothing.

        Args:
            reason: Why, given to the error the scopes raise.
        """
        if self.reason is not None:
            return
        self.reason = reason
        self._tripped.set()
        for cancel in list(self._cancels):
            cancel()

    async def wait(self) -> None:
        """Wait until the wire is tripped, if it was not already."""
        await self._tripped.wait()

    @asynccontextmanager
    async def guarding(
        self, error: Callable[[str], Exception], *, may_trip: Callable[[BaseException], bool] = lambda _error: False
    ) -> AsyncGenerator[None]:
        """Run a block until the wire is tripped.

        Args:
            error: The error to raise for the reason the wire was tripped with.
            may_trip: Tells of an error the block raised whether it may come of what
                trips the wire, so that it waits `GRACE` seconds for it.

        Yields:
            Nothing; the block runs in the current task.

        Raises:
            Exception: The error for the reason, once the wire is tripped, before
                the block or while it runs.
        """
        if self.reason is not None:
            raise error(self.reason)
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("a Tripwire guards code that runs in a task")
        cancelling = task.cancelling()
        cancelled = False

        def cancel() -> None:
            nonlocal cancelled
            cancelled = True
            task.cancel()

        self._cancels.add(cancel)
        try:
            yield
        except asyncio.CancelledError as exc:
            self._cancels.discard(cancel)
            if cancelled and task.uncancel() <= cancelling and self.reason is not None:
                raise error(self.reason) from exc
            raise
        except Exception as exc:
            self._cancels.discard(cancel)
            if cancelled:
                task.uncancel()  # tripped while the block failed of itself
            elif self.reason is None and may_trip(exc):
                await self._grace()
            if self.reason is not None:
                raise error(self.reason) from exc
            raise
        finally:
            self._cancels.discard(cancel)
        if cancelled:
            task.uncancel()  # tripped once the block was done

    async def _grace(self) -> None:
        await first_tripped([self], within=GRACE)


async def first_tripped(wires: Sequence[Tripwire], *, within: float) -> Tripwire | None:
    """Wait until one of some tripwires is tripped, if it was not already.

    Args:
        wires: The tripwires.
        within: Seconds to wait at most.

    Returns:
        The first tripwire found tripped, or `None` if none was within the time.
    """
    for wire in wires:
        if wire.reason is not None:
            return wire
    waits = [asyncio.ensure_future(wire.wait()) for wire in wires]
    try:
        await asyncio.wait(waits, timeout=within, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
    return next((wire for wire in wires if wire.reason is not None), None)
