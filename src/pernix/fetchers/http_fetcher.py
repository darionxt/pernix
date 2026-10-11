from __future__ import annotations

from typing import TYPE_CHECKING, Self, cast

from curl_cffi.requests import AsyncSession

from pernix.headers import Headers
from pernix.response import Response

if TYPE_CHECKING:
    from curl_cffi.requests.session import HttpMethod

    from pernix.request import Request

__all__ = ["HTTPFetcher"]


class HTTPFetcher:
    """Fetches Requests over HTTP with curl-cffi.

    Use it as an async context manager: the underlying session is created on entry,
    bound to the running event loop, and closed on exit.
    """

    def __init__(self, *, max_clients: int = 16) -> None:
        """Configure the fetcher.

        Args:
            max_clients: The maximum number of requests in flight at once.
        """
        self._max_clients = max_clients
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        self._session = AsyncSession(max_clients=self._max_clients, trust_env=False)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def fetch(self, request: Request) -> Response:
        """Send a Request and read the whole response body.

        Args:
            request: The Request to send.

        Returns:
            The Response, whatever its status code.

        Raises:
            RuntimeError: The fetcher was not entered with `async with`.
            curl_cffi.requests.exceptions.RequestException: The transfer failed.
        """
        if self._session is None:
            raise RuntimeError("HTTPFetcher must be entered with 'async with' before fetching")
        result = await self._session.request(
            cast("HttpMethod", request.method),
            request.url,
            headers=request.headers.items(),
            data=request.body,
        )
        return Response(
            url=result.url,
            status=result.status_code,
            headers=Headers((k, v) for k, v in result.headers.multi_items() if v is not None),
            body=result.content,
            request=request,
        )
