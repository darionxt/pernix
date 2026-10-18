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

__all__ = ["FetchPlan", "FetchSpider", "InvalidURL"]


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
    are Requests that were ignored or dropped, and URLs that cannot be fetched. A
    configuration error, such as a browser engine that is not installed, stops the
    Run at once, as no fetch can succeed until it is fixed; so does a closed stdout.

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

    async def start(self) -> AsyncIterator[Request]:
        """Yield the Requests of the plan, reporting the URLs that cannot be fetched instead.

        Yields:
            The Requests, which fail into `failed()`.
        """
        output = self._plan.output
        self.crawler.events.on(RequestIgnored, output.ignored)
        self.crawler.events.on(RequestDropped, output.dropped)
        for entry in self._plan.entries:
            if isinstance(entry, InvalidURL):
                output.failed(entry.url, entry.error)
                self._stop_if_closed()
            else:
                yield entry.replace(errback=self.failed)

    async def parse(self, response: Response) -> None:
        """Report a Response.

        Args:
            response: The Response.
        """
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
        self._plan.output.failed(request.url, error)
        self._stop_if_closed()

    def _stop_if_closed(self) -> None:
        if self._plan.output.closed:
            self.crawler.stop("force")
