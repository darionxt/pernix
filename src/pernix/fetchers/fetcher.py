from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["Fetcher"]


class Fetcher(Protocol):
    """A transport that turns a Request into a Response."""

    async def fetch(self, request: Request) -> Response:
        """Fetch one Request.

        Args:
            request: The Request to fetch.

        Returns:
            The Response, whatever its status code.
        """
        ...
