"""The Spider `pernix fetch` runs: it fetches the URLs it is given and reports each as it ends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.errors import CONFIGURATION_ERRORS
from pernix.events import RequestDropped, RequestIgnored
from pernix.spider import Spider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix.cli.fetch_output import FetchOutput
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["FetchPlan", "FetchSpider", "InvalidURL", "NotFetched"]

_ENTRY = "pernix.fetch.entry"
"""The meta key of a Request's place among the entries, which tells which entry a report is for."""


class NotFetched(Exception):
    """A URL the Run ended before reporting, as when it drained for want of proxies."""


@dataclass(frozen=True, slots=True)
class InvalidURL:
    """A URL that cannot be fetched, as a line of `-i FILE` may be.

    Attributes:
        url: The URL, as given.
        error: Why it cannot be fetched.
    """

    url: str
    error: Exception


class FetchPlan:
    """What one `pernix fetch` fetches, and where it reports each URL.

    It is the Spider's argument, given as it is: a plain class, which Spider
    arguments are never converted to.

    Attributes:
        entries: The Requests to fetch, in order, and the URLs that cannot be.
        output: Where each is reported.
    """

    __slots__ = ("entries", "output")

    def __init__(self, entries: tuple[Request | InvalidURL, ...], output: FetchOutput) -> None:
        """Plan the fetches.

        Args:
            entries: The Requests to fetch, in order, and the URLs that cannot be.
            output: Where each is reported.
        """
        self.entries = entries
        self.output = output


class FetchSpider(Spider):
    """Fetches the Requests of a `FetchPlan` as one Run, reporting each as it ends.

    A Response is reported, whatever its status, and so is the last one once the
    retries for its status ran out; a failure is reported with its error, and so
    are Requests that were ignored or dropped, and URLs that cannot be fetched.
    Those the Run ended before, as when it drained for want of proxies, are left in
    `unfinished`. A configuration error, such as a browser engine that is not
    installed, stops the Run at once, as no fetch can succeed until it is fixed; so
    does a closed stdout.

    Attributes:
        configuration_error: The configuration error that stopped the Run, if any.
    """

    name = "fetch"

    def __init__(self, plan: FetchPlan) -> None:
        """Create the Spider.

        Args:
            plan: What to fetch.
        """
        self._plan = plan
        self.configuration_error: Exception | None = None
        self._unreported: dict[int, str] = {}
        """The URLs of the Requests yielded and not reported yet, by their place among the entries."""

    @property
    def unfinished(self) -> list[str]:
        """The URLs of the Requests no report was made for, in the order they were given."""
        return list(self._unreported.values())

    async def start(self) -> AsyncIterator[Request]:
        """Yield the Requests of the plan, reporting the URLs that cannot be fetched instead.

        Yields:
            The Requests, which fail into `failed()`.
        """
        output = self._plan.output
        self.crawler.events.on(RequestIgnored, self._ignored)
        self.crawler.events.on(RequestDropped, self._dropped)
        for index, entry in enumerate(self._plan.entries):
            if isinstance(entry, InvalidURL):
                output.failed(entry.url, entry.error)
                self._stop_if_closed()
            else:
                self._unreported[index] = entry.url
                yield entry.replace(errback=self.failed, meta={**entry.meta, _ENTRY: index})

    async def parse(self, response: Response) -> None:
        """Report a Response.

        Args:
            response: The Response.
        """
        self._reported(response.request)
        self._plan.output.response(response)
        self._stop_if_closed()

    async def failed(self, request: Request, error: Exception) -> None:
        """Report a Request that failed, or stop the Run for a configuration error.

        Args:
            request: The Request.
            error: Why it failed.
        """
        if isinstance(error, CONFIGURATION_ERRORS):
            if self.configuration_error is None:
                self.configuration_error = error
            self.crawler.stop("force")
            return
        self._reported(request)
        try:
            self._plan.output.failed(request.url, error)
        except Exception as exc:  # as reporting a Response can fail, which no errback would hear of
            self._plan.output.failed(request.url, exc)
        self._stop_if_closed()

    def _ignored(self, event: RequestIgnored) -> None:
        self._reported(event.request)
        self._plan.output.ignored(event)

    def _dropped(self, event: RequestDropped) -> None:
        self._reported(event.request)
        self._plan.output.dropped(event)

    def _reported(self, request: Request) -> None:
        self._unreported.pop(request.meta.get(_ENTRY, -1), None)

    def _stop_if_closed(self) -> None:
        if self._plan.output.closed:
            self.crawler.stop("force")
