"""Telling what the proxy of `network.public_only` did not connect a page to: its refusals, and the error of its navigation."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

    from patchright.async_api import Page, WebSocket
    from patchright.async_api import Request as PageRequest

    from pernix.errors import FetchError
    from pernix.fetchers.address_guard_proxy import AddressGuardProxy

__all__ = ["ProxyRefusals"]

_PROXY_FAILED = "net::ERR_SOCKS_CONNECTION_FAILED"
"""What fails in the browser that the address guard's proxy did not connect, whatever the reason."""


class ProxyRefusals:
    """Watches the requests of a page's context, and its WebSockets, for those the address guard's proxy refused."""

    def __init__(self, page: Page, proxy: AddressGuardProxy, refused: Callable[[str, str], None]) -> None:
        """Watch a page.

        Args:
            page: The page.
            proxy: The proxy its browser sends its traffic through.
            refused: Told of each request the proxy refused, with its URL and why.
        """
        self._page = page
        self._proxy = proxy
        self._refused = refused
        page.context.on("requestfailed", self._on_failed)
        page.on("websocket", self._on_websocket)

    def stop(self) -> None:
        """Stop watching the context, which may outlive the page."""
        self._page.context.remove_listener("requestfailed", self._on_failed)

    def navigation_error(
        self, failed: PageRequest | None, url: str, *, error: str = "", navigated: bool = False
    ) -> FetchError | None:
        """The error of a navigation of the main frame that failed as the proxy did not connect it.

        Args:
            failed: The navigation that failed, if the page told of one.
            url: The URL of the Request loaded.
            error: The message of the error loading it raised, if it did, which
                names that URL.
            navigated: Whether the navigation came after the page had loaded.

        Returns:
            The error, a `NetworkBlocked` for a host refused, worded as the HTTP
            Fetcher's; `None` for a navigation that did not fail so.
        """
        if failed is not None and _PROXY_FAILED in (failed.failure or ""):
            target, previous = failed.url, failed.redirected_from
            hop = target if previous is None else f"the redirect from {previous.url} to {target}"
        elif failed is None and _PROXY_FAILED in error:
            target = hop = url
        else:
            return None
        failure = self._proxy.failure(target)
        if failure is None:
            return None
        return failure.error(f"{hop}, to which the page of {url} navigated" if navigated else hop)

    def _on_failed(self, request: PageRequest) -> None:
        self._tell(request.url, request.failure or "")

    def _on_websocket(self, websocket: WebSocket) -> None:
        websocket.on("socketerror", lambda error: self._tell(websocket.url, str(error)))

    def _tell(self, url: str, failure: str) -> None:
        """Tell of a request the proxy refused; one that failed otherwise is not told."""
        if _PROXY_FAILED not in failure:
            return
        found = self._proxy.failure(url)
        if found is not None and found.refused:
            self._refused(url, found.reason)
