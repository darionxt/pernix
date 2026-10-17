"""Starting patchright and its browser for the browser Fetcher, under the network guard if it is on."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import unquote, urlsplit

from pernix.errors import ComponentError
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.refusing_proxy import RefusingProxy

if TYPE_CHECKING:
    from patchright.async_api import Browser, BrowserContext, Playwright, ProxySettings

    from pernix.fetchers.browser_settings import BrowserSettings

__all__ = ["LOOPBACK_BYPASS", "LaunchedBrowser", "launch_browser"]

LOOPBACK_BYPASS = "<-loopback>,localhost,*.localhost,127.0.0.1/8,[::1]"
"""Proxy bypass rules for loopback hosts and nothing else; Chrome's implicit ones would also bypass link-local addresses."""

_EXTRA_HINT = "rendering needs patchright; install it with: pip install 'pernix[browser]'"
_ENGINE_HINT = "the browser engine is not installed; install it with: patchright install chromium"


@dataclass(slots=True)
class LaunchedBrowser:
    """A running browser and the patchright driver that runs it.

    Attributes:
        playwright: The patchright driver.
        browser: The browser.
        guarded: Whether it was launched under the network guard, so that only
            loopback hosts are reached without a proxy of the Request's own.
        refusing_proxy: Where a guarded browser sends everything else.
    """

    playwright: Playwright
    browser: Browser
    guarded: bool
    refusing_proxy: RefusingProxy | None

    async def new_context(self, *, proxy: str | None = None) -> BrowserContext:
        """Create a context of its own for one fetch.

        Args:
            proxy: The proxy URL every request of the context goes through, loopback
                ones included; without one, the browser's own applies.

        Returns:
            The context, without pages.
        """
        if not proxy:
            return await self.browser.new_context()
        return await self.browser.new_context(proxy=_proxy_settings(proxy))

    async def close(self) -> None:
        """Close the browser, stop the driver, and stop the refusing proxy."""
        try:
            await self.browser.close()
        finally:
            try:
                await self.playwright.stop()
            finally:
                if self.refusing_proxy is not None:
                    await self.refusing_proxy.close()


async def launch_browser(settings: BrowserSettings) -> LaunchedBrowser:
    """Start patchright and launch a browser.

    The browser is Chrome for Testing, in the new headless mode unless
    `browser.headless` is off, with `browser.launch_options` over those defaults.
    Under the network guard, every connection to a host other than a loopback one
    goes to a proxy that refuses it, whatever `launch_options` say.

    Args:
        settings: The browser settings.

    Returns:
        The running browser.

    Raises:
        pernix.errors.ComponentError: patchright or its browser engine is not
            installed.
    """
    try:
        from patchright.async_api import Error, async_playwright  # an optional dependency, imported when needed
    except ImportError as exc:
        raise ComponentError(_EXTRA_HINT) from exc
    options: dict[str, Any] = {"channel": "chromium", **settings.launch_options, "headless": settings.headless}
    refusing_proxy: RefusingProxy | None = None
    playwright: Playwright | None = None
    try:
        if current_guard() is not None:
            refusing_proxy = RefusingProxy()
            await refusing_proxy.start()
            options["proxy"] = {"server": refusing_proxy.url, "bypass": LOOPBACK_BYPASS}
        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(**options)
        except Error as exc:
            if "executable doesn't exist" in str(exc).lower():
                raise ComponentError(_ENGINE_HINT) from exc
            raise
    except BaseException:
        if playwright is not None:
            await playwright.stop()
        if refusing_proxy is not None:
            await refusing_proxy.close()
        raise
    return LaunchedBrowser(playwright, browser, refusing_proxy is not None, refusing_proxy)


def _proxy_settings(proxy: str) -> ProxySettings:
    """A proxy URL as patchright takes it: the server apart from the credentials."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    settings: ProxySettings = {"server": f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"}
    if parts.username is not None:
        settings["username"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    return settings
