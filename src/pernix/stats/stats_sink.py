from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pernix.events import StatsSnapshot

__all__ = ["StatsSink"]


class StatsSink(Protocol):
    """Receives every stats snapshot of a Run, to report it elsewhere.

    Register Stats Sinks with the `stats.sinks` setting, as `{sink: order}`. The
    Crawler builds each one like an Extension, opens it before the Run and closes it
    after the final snapshot, and subscribes `push` to `StatsSnapshot`, so a sink that
    raises or times out is logged and does not affect the Run.

    Example:
        >>> class PrintSink:
        ...     def push(self, snapshot: StatsSnapshot) -> None:
        ...         print(snapshot.final, snapshot.stats["items.scraped"])
    """

    def push(self, snapshot: StatsSnapshot) -> object:
        """Report one snapshot; may be sync or async.

        Args:
            snapshot: The stats, and whether they are the final ones of the Run.
        """
        ...
