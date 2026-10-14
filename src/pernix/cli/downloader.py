from __future__ import annotations

from typing import TYPE_CHECKING, Self

from pernix.component_lifecycle import ComponentLifecycle
from pernix.response import expect_response
from pernix.run_assembly import DownloadAssembly

if TYPE_CHECKING:
    from types import TracebackType

    from pernix.crawler import Crawler
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["Downloader"]


class Downloader:
    """Fetches Requests one at a time outside a Run, as `pernix fetch` and `pernix shell` do.

    The Fetchers and the download Middleware of the Crawler's settings are built when
    it is created, and subscribed and opened as an async context manager. Requests go
    through the download chain once: there are no retries, no Frontier, and no
    politeness delays.
    """

    def __init__(self, crawler: Crawler) -> None:
        """Build the Fetchers and the download Middleware.

        Args:
            crawler: Builds the components, with the settings to use.

        Raises:
            pernix.errors.ComponentError: A component cannot be built, or does not fit
                its role.
            pernix.errors.SettingsError: Two Fetchers claim the same URL scheme.
        """
        self._crawler = crawler
        self._assembly = DownloadAssembly.build(crawler)
        self._lifecycle: ComponentLifecycle | None = None

    async def __aenter__(self) -> Self:
        lifecycle = ComponentLifecycle()
        try:
            for component in self._assembly.components:
                self._crawler.events.subscribe_methods(component)
                await lifecycle.open(component)
        except BaseException as exc:
            await lifecycle.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        self._lifecycle = lifecycle
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        lifecycle, self._lifecycle = self._lifecycle, None
        if lifecycle is not None:
            await lifecycle.__aexit__(exc_type, exc, traceback)

    async def fetch(self, request: Request) -> Response:
        """Fetch a Request through the download chain.

        Args:
            request: The Request.

        Returns:
            Its Response.

        Raises:
            RuntimeError: The Downloader is not open.
            LookupError: The Request names a Fetcher that is not registered.
            pernix.errors.FetchError: Fetching failed.
            pernix.errors.IgnoreRequest: A download Middleware dropped the Request.
        """
        if self._lifecycle is None:
            raise RuntimeError("the Downloader is not open; use it with `async with`")
        return expect_response(await self._assembly.download(request), "the download chain")
