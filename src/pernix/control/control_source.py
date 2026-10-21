"""The control source protocol: the state a Platform wants a Run in, read from outside its process."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = ["CONTROL_STATES", "ControlSource", "ControlState"]

type ControlState = Literal["running", "paused", "drain", "force"]
"""The state a Platform wants a Run in: running, paused, drained, or force-stopped."""

CONTROL_STATES: tuple[ControlState, ...] = ("running", "paused", "drain", "force")
"""The control states, from the least restrictive to the most."""


class ControlSource(Protocol):
    """Tells the state a Platform wants a Run in, such as from a file or a key in a store.

    Register control sources with `control.sources`, as `{source: order}`;
    `control.file` adds a `pernix.control.ControlFile` before them. The Crawler
    builds each one like an Extension and opens it before the Run, and
    `pernix.control.RunControl` reads them all when the Run starts, then every
    `control.interval` seconds, and applies the most restrictive state they tell
    whenever it changes. Pernix only reads the state; writing it is the Platform's.
    A source that raises, takes longer than the interval, or tells something else
    is logged, and the state it told before is kept.

    Example:
        >>> class FromEnvironment:
        ...     def state(self) -> ControlState:
        ...         return os.environ.get("CRAWL_STATE", "running")
    """

    def state(self) -> ControlState | Awaitable[ControlState]:
        """Tell the state wanted now; may be sync or async.

        A sync one runs on the event loop, so it must not block.

        Returns:
            `"running"`, `"paused"`, `"drain"`, or `"force"`.
        """
        ...
