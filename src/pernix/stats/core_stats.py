from __future__ import annotations

import re
import sys
from typing import TYPE_CHECKING

from pernix.errors import IgnoreRequest, Retry
from pernix.events import (
    ItemDropped,
    ItemError,
    ItemScraped,
    RequestDropped,
    RequestFailed,
    RequestIgnored,
    RequestRetried,
    RequestScheduled,
    ResponseReceived,
    on,
)
from pernix.response import Response

if TYPE_CHECKING:
    from pernix.middlewares import Download
    from pernix.request import Request
    from pernix.stats.stats import Stats

__all__ = ["CoreStats"]

_COUNTERS = (
    "requests.enqueued",
    "requests.dropped",
    "requests.retried",
    "requests.ignored",
    "requests.failed",
    "downloads.responses",
    "downloads.bytes",
    "downloads.errors",
    "responses.received",
    "items.scraped",
    "items.dropped",
    "items.errors",
    "items.retried",
    "items.failures_lost",
    "items.discarded",
)

_UNSAFE_LABEL = re.compile(r"[^\w-]+")


class CoreStats:
    """Counts what happens to the Requests, Responses, and Items of a Run.

    The Crawler subscribes it to the Run's Events and wraps the download chain with
    `count_downloads()`. Every counter below starts at 0; the breakdowns appear with
    their first count. A label, such as a reason, has its runs of characters other
    than letters, digits, `_`, and `-` replaced by `_`.

    - `requests.enqueued`: Requests that entered the Frontier.
    - `requests.dropped`, `requests.dropped_by_reason.<reason>`: Requests vetoed, or
      dropped as duplicates.
    - `requests.retried`, `requests.ignored`: Attempts retried; Requests ignored.
    - `requests.failed`, `requests.failed_by_error.<ErrorType>`: Requests that failed
      for good.
    - `downloads.responses`, `downloads.by_status.<code>`, `downloads.bytes`: Responses
      the download chain returned, including those retried afterwards and those of
      Inline Fetches, and their body bytes; the Inline Fetch counts streamed bodies
      as they are read.
    - `downloads.errors`, `downloads.errors_by_type.<ErrorType>`: Exceptions it raised,
      other than `IgnoreRequest` and `Retry`.
    - `responses.received`: Responses handed to Callbacks.
    - `items.scraped`, `items.dropped`, `items.errors`: Items stored, dropped, or failed.
    - `items.retried`, `items.failures_lost`, `items.discarded`: Pipeline calls retried,
      failed Items a failure Pipeline lost, and Items a Run stopped before they were
      processed; the ItemProcessor counts them.
    """

    def __init__(self, stats: Stats) -> None:
        """Start every counter at 0.

        Args:
            stats: Where to count.
        """
        self._stats = stats
        self._offered = 0
        self._dropped = 0
        for key in _COUNTERS:
            stats.set(key, 0)

    def count_downloads(self, download: Download) -> Download:
        """Wrap a download chain so that its Responses and errors are counted.

        Args:
            download: The download chain.

        Returns:
            The same chain, counted.
        """
        stats = self._stats

        async def counted(request: Request) -> Response:
            try:
                response = await download(request)
            except (IgnoreRequest, Retry):
                raise
            except Exception as exc:
                stats.inc("downloads.errors")
                stats.inc(f"downloads.errors_by_type.{_label(type(exc).__name__)}")
                raise
            _count_response(stats, response)
            return response

        return counted

    @on(RequestScheduled, priority=-sys.maxsize)
    def offered(self, _event: RequestScheduled) -> None:
        """Count a Request offered to the Frontier; it runs before every gate."""
        self._offered += 1
        self._stats.set("requests.enqueued", self._offered - self._dropped)

    @on(RequestDropped)
    def dropped(self, event: RequestDropped) -> None:
        """Count a dropped Request by its reason."""
        self._dropped += 1
        self._stats.set("requests.enqueued", self._offered - self._dropped)
        self._stats.inc("requests.dropped")
        self._stats.inc(f"requests.dropped_by_reason.{_label(event.reason)}")

    @on(RequestRetried)
    def retried(self, _event: RequestRetried) -> None:
        """Count a retried attempt."""
        self._stats.inc("requests.retried")

    @on(RequestIgnored)
    def ignored(self, _event: RequestIgnored) -> None:
        """Count an ignored Request."""
        self._stats.inc("requests.ignored")

    @on(RequestFailed)
    def failed(self, event: RequestFailed) -> None:
        """Count a Request that failed for good, by its error type."""
        self._stats.inc("requests.failed")
        self._stats.inc(f"requests.failed_by_error.{_label(type(event.error).__name__)}")

    @on(ResponseReceived)
    def received(self, _event: ResponseReceived) -> None:
        """Count a Response handed to its Callback."""
        self._stats.inc("responses.received")

    @on(ItemScraped)
    def scraped(self, _event: ItemScraped) -> None:
        """Count a stored Item."""
        self._stats.inc("items.scraped")

    @on(ItemDropped)
    def item_dropped(self, _event: ItemDropped) -> None:
        """Count a dropped Item."""
        self._stats.inc("items.dropped")

    @on(ItemError)
    def item_error(self, _event: ItemError) -> None:
        """Count an Item that failed."""
        self._stats.inc("items.errors")


def _label(text: str) -> str:
    return _UNSAFE_LABEL.sub("_", text).strip("_") or "_"


def _count_response(stats: Stats, response: object) -> None:
    if isinstance(response, Response):  # the Engine reports anything else
        stats.inc("downloads.responses")
        stats.inc(f"downloads.by_status.{response.status}")
        stats.inc("downloads.bytes", len(response.body))
