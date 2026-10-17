from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar, Protocol

if TYPE_CHECKING:
    from contextlib import AbstractAsyncContextManager

    from pernix.fetchers.streamed_response import StreamedResponse
    from pernix.request import Request
    from pernix.response import Response
    from pernix.session import Session

__all__ = ["Fetcher", "StreamingFetcher"]


class Fetcher(Protocol):
    """A transport that turns a Request into a Response.

    Register Fetchers by name with the `fetchers` setting, such as
    `{"http": HTTPFetcher}`; `Request(fetcher=name)` selects one explicitly. Like
    other components, a Fetcher gets services injected and is opened and closed with
    the Run, as an async context manager or through `open()` / `close()`.

    A Request sent under a Session is fetched with `session=`, given only for such
    Requests: a Fetcher that never sends Requests under a Session may leave the
    parameter out, and one that cannot should refuse them.

    Attributes:
        schemes: The URL schemes it fetches when a Request names no Fetcher and needs
            no rendering, such as `{"http", "https"}`. Two Fetchers may not share a
            scheme. Leave it empty for a Fetcher selected only by name.
    """

    schemes: ClassVar[frozenset[str]]

    async def fetch(self, request: Request, *, session: Session | None = None) -> Response:
        """Fetch one Request.

        Args:
            request: The Request to fetch.
            session: The Session to send it under, if any: its cookies go with the
                Request, and the cookies set during the fetch are kept in it.

        Returns:
            The Response, whatever its status code, with `session` naming the
            Session it was sent under.
        """
        ...


class StreamingFetcher(Fetcher, Protocol):
    """A Fetcher that can also give a body as it arrives, for `Crawler.stream()`.

    Without `stream()`, a streamed fetch reads the whole body with `fetch()` first.
    """

    def stream(
        self, request: Request, *, max_bytes: int | None = None, session: Session | None = None
    ) -> AbstractAsyncContextManager[StreamedResponse]:
        """Fetch one Request, giving its body as it arrives.

        Args:
            request: The Request to fetch.
            max_bytes: Bytes the body may have at most; `None` for the Fetcher's own
                limit, and 0 for no limit.
            session: The Session to send it under, if any, as for `fetch()`.

        Returns:
            An async context manager giving the Response, whose body is read once,
            and ending the transfer on exit.
        """
        ...
