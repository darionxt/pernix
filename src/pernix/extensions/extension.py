from __future__ import annotations

__all__ = ["Extension"]


class Extension:
    """Base class for components that subscribe to Events and have their own lifecycle.

    Register Extensions with the `extensions` setting. The Crawler builds each one
    (injecting `Crawler`, `Settings`, `EventBus`, `Stats`, or Sections into annotated
    `__init__` parameters), subscribes its methods marked with `events.on()`, and
    calls `open()` before the Run and `close()` after it, in reverse order.

    Example:
        >>> class Notify(Extension):
        ...     @events.on(events.SpiderClosed)
        ...     async def closed(self, event: events.SpiderClosed) -> None:
        ...         await send(f"{event.spider.name} {event.reason}")
    """

    async def open(self) -> None:
        """Acquire resources before the Run starts. Does nothing by default."""

    async def close(self) -> None:
        """Release resources after the Run ends. Does nothing by default."""
