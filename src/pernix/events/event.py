from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import ClassVar

__all__ = ["Dispatch", "Event", "Veto"]


class Dispatch(Enum):
    """How the subscribers of an Event are run."""

    OBSERVE = "observe"
    """Awaited in order; an exception is logged and isolated, and return values are ignored."""

    GATE = "gate"
    """Awaited in order until one returns a `Veto`, which short-circuits the rest."""

    TRANSFORM = "transform"
    """Awaited in order; each may return a replacement Event that the next one receives."""


@dataclass(frozen=True, slots=True)
class Veto:
    """Returned by a gate subscriber to reject the Event.

    Attributes:
        reason: Why the Event was rejected, for logs and the dropped Event.
    """

    reason: str


@dataclass(frozen=True)
class Event:
    """Base class for typed notifications emitted during a Run.

    Subclasses are frozen dataclasses and declare how they are dispatched through
    class variables.

    Attributes:
        dispatch: How subscribers are run; see `Dispatch`.
        fail_closed: For gate Events, whether a subscriber that raises or times out
            vetoes the Event (`True`) or is ignored (`False`).
        reverse: Run subscribers in reverse order, as for shutdown Events, so that
            what was set up first is torn down last.

    Example:
        >>> @dataclass(frozen=True)
        ... class PageSaved(Event):
        ...     path: str
    """

    dispatch: ClassVar[Dispatch] = Dispatch.OBSERVE
    fail_closed: ClassVar[bool] = False
    reverse: ClassVar[bool] = False
