"""Page methods: Spider methods a rendered Request runs on its page, for what the declarative actions cannot do."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import Page

__all__ = ["PageCallable", "PageMethod"]

type PageCallable = Callable[[Page], Awaitable[Any]]
"""An async method taking the live patchright `Page`, such as `async def before_goto(self, page)` of a Spider."""


@dataclass(frozen=True, slots=True)
class PageMethod:
    """A Spider method a rendered Request runs on its page, as `Render(setup=...)` or `Render(action=...)`.

    `Render` takes the method itself too; wrap it to mark it optional, or to limit
    how long it may take. Like the Callback of a Request, it is persisted by its
    name, so it must be a method of the Spider for a Request to be stored; its
    return value is ignored.

    Attributes:
        method: The method, `async def method(self, page)`, given the patchright
            `Page`.
        optional: When it fails, log the failure as a warning and go on, as though
            it had not run.
        timeout: Seconds it may take; `None` for no limit of its own, as
            `browser.deadline` still limits the whole fetch.

    Example:
        >>> Render(action=PageMethod(self.close_banner, optional=True, timeout=5))
    """

    method: PageCallable
    optional: bool = False
    timeout: float | None = None

    def __post_init__(self) -> None:
        if not inspect.iscoroutinefunction(self.method):
            raise TypeError(
                f"PageMethod: {self.method!r} is not an async method; page methods are Spider methods "
                "such as 'async def after_load(self, page)'"
            )
        if self.timeout is not None and not self.timeout > 0:
            raise ValueError(f"PageMethod: timeout must be positive, got {self.timeout}")

    @property
    def name(self) -> str:
        """The method's name as messages give it, such as `Books.after_load`."""
        return getattr(self.method, "__qualname__", repr(self.method))
