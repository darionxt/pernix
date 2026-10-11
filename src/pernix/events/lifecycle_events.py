from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar

from pernix.events.event import Dispatch, Event

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.request import Request
    from pernix.response import Response
    from pernix.spider import Spider

__all__ = [
    "ItemDropped",
    "ItemError",
    "ItemScraped",
    "RequestDropped",
    "RequestFailed",
    "RequestScheduled",
    "ResponseReceived",
    "SpiderClosed",
    "SpiderIdle",
    "SpiderOpened",
    "StatsSnapshot",
]


@dataclass(frozen=True)
class SpiderOpened(Event):
    """The Run started: components are open and no Request has been fetched yet.

    Attributes:
        spider: The Spider being run.
    """

    spider: Spider


@dataclass(frozen=True)
class SpiderIdle(Event):
    """The Frontier is empty and nothing is in flight.

    Attributes:
        spider: The Spider being run.
    """

    spider: Spider


@dataclass(frozen=True)
class SpiderClosed(Event):
    """The Run ended. Subscribers run in reverse order.

    Attributes:
        spider: The Spider that was run.
        reason: `"finished"`, `"failed"`, or `"cancelled"`.
    """

    reverse: ClassVar[bool] = True

    spider: Spider
    reason: str


@dataclass(frozen=True)
class RequestScheduled(Event):
    """A Request is about to enter the Frontier. A gate: any subscriber can veto it.

    A subscriber that raises or times out does not block the Request (fail-open).

    Attributes:
        request: The Request.
    """

    dispatch: ClassVar[Dispatch] = Dispatch.GATE

    request: Request


@dataclass(frozen=True)
class RequestDropped(Event):
    """A Request did not enter the Frontier.

    Attributes:
        request: The Request.
        reason: Why it was dropped, for example `"duplicate"` or a veto reason.
    """

    request: Request
    reason: str


@dataclass(frozen=True)
class RequestFailed(Event):
    """Fetching a Request, or its Callback, raised.

    Attributes:
        request: The Request.
        error: The exception.
    """

    request: Request
    error: Exception


@dataclass(frozen=True)
class ResponseReceived(Event):
    """A Response was fetched and is about to be passed to its Callback.

    Attributes:
        response: The Response.
    """

    response: Response


@dataclass(frozen=True)
class ItemScraped(Event):
    """An Item was stored.

    Attributes:
        item: The Item.
        response: The Response whose Callback produced it.
    """

    item: Any
    response: Response


@dataclass(frozen=True)
class ItemDropped(Event):
    """A Pipeline dropped an Item.

    Attributes:
        item: The Item.
        response: The Response whose Callback produced it.
        reason: Why it was dropped.
    """

    item: Any
    response: Response
    reason: str


@dataclass(frozen=True)
class ItemError(Event):
    """Processing or storing an Item raised.

    Attributes:
        item: The Item.
        response: The Response whose Callback produced it.
        error: The exception.
    """

    item: Any
    response: Response
    error: Exception


@dataclass(frozen=True)
class StatsSnapshot(Event):
    """A periodic copy of the Run's stats.

    Attributes:
        stats: Stat name to value.
    """

    stats: Mapping[str, Any]
