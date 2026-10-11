from __future__ import annotations

import inspect
import logging
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType

__all__ = ["ComponentLifecycle"]

logger = logging.getLogger(__name__)


class ComponentLifecycle:
    """Opens components in order and closes them in reverse.

    A component is opened with `__aenter__` if it is an async context manager, and
    otherwise with its `open()` method if it has one; it is closed with `__aexit__`
    or `close()`. Sync and async methods are both accepted.

    Every close step runs even when earlier ones fail; their exceptions are raised
    together as an `ExceptionGroup`. A component whose opening failed is not closed.

    Example:
        >>> async with ComponentLifecycle() as lifecycle:
        ...     await lifecycle.open(first)
        ...     await lifecycle.open(second)  # if this fails, `first` is closed
        ...     ...  # on exit, `second` is closed, then `first`
    """

    def __init__(self) -> None:
        """Create a lifecycle with no open components."""
        self._closers: list[tuple[object, Callable[[], object]]] = []

    async def open(self, component: object) -> None:
        """Open a component and remember how to close it.

        Args:
            component: Any object; one with neither context-manager methods nor
                `open()` / `close()` is accepted and needs no closing.
        """
        enter = getattr(component, "__aenter__", None)
        exit_ = getattr(component, "__aexit__", None)
        if callable(enter) and callable(exit_):
            await _resolve(enter())
            self._closers.append((component, lambda: exit_(None, None, None)))
            return
        opener = getattr(component, "open", None)
        if callable(opener):
            await _resolve(opener())
        closer = getattr(component, "close", None)
        if callable(closer):
            self._closers.append((component, closer))

    async def aclose(self) -> None:
        """Close every open component, most recently opened first.

        Raises:
            ExceptionGroup: One or more components failed to close; each exception
                has a note naming its component.
        """
        errors: list[Exception] = []
        while self._closers:
            component, closer = self._closers.pop()
            try:
                await _resolve(closer())
            except Exception as exc:
                exc.add_note(f"while closing {component!r}")
                errors.append(exc)
        if errors:
            raise ExceptionGroup("closing components failed", errors)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            await self.aclose()
        except ExceptionGroup as group:
            if exc is None:
                raise
            if isinstance(exc, Exception):
                raise ExceptionGroup(
                    "the Run failed, and closing components failed", [exc, *group.exceptions]
                ) from None
            # Never replace a cancellation or an interrupt: report the close errors instead.
            logger.error("Closing components failed during %s", type(exc).__name__, exc_info=group)


async def _resolve(result: object) -> object:
    return await result if inspect.isawaitable(result) else result
