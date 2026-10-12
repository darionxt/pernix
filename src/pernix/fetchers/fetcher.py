from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["Fetcher"]


class Fetcher(Protocol):
    """A transport that turns a Request into a Response.

    Register Fetchers by name with the `fetchers` setting, such as
    `{"http": HTTPFetcher}`; `Request(fetcher=name)` selects one explicitly. Like
    other components, a Fetcher gets services injected and is opened and closed with
    the Run, as an async context manager or through `open()` / `close()`.

    Attributes:
        schemes: The URL schemes it fetches when a Request names no Fetcher and needs
            no rendering, such as `{"http", "https"}`. Two Fetchers may not share a
            scheme. Leave it empty for a Fetcher selected only by name.
    """

    schemes: ClassVar[frozenset[str]]

    async def fetch(self, request: Request) -> Response:
        """Fetch one Request.

        Args:
            request: The Request to fetch.

        Returns:
            The Response, whatever its status code.
        """
        ...
