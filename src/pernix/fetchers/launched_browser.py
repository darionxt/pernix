"""Starting patchright and its browser for the browser Fetcher, under the network guard if it is on, and probing it."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlsplit

from pernix.errors import ComponentError
from pernix.fetchers.address_guard_proxy import AddressGuardProxy
from pernix.fetchers.browser_processes import BrowserProcesses
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.refusing_proxy import RefusingProxy

if TYPE_CHECKING:
    from collections.abc import Mapping

    from patchright._impl._api_structures import StorageState
    from patchright.async_api import Browser, BrowserContext, CDPSession, Playwright, ProxySettings
    from patchright.async_api._context_manager import PlaywrightContextManager

    from pernix.fetchers.address_guard import AddressGuard
    from pernix.fetchers.browser_settings import BrowserSettings

__all__ = ["LOOPBACK_BYPASS", "LaunchedBrowser", "launch_browser", "probe_browser", "require_patchright"]

LOOPBACK_BYPASS = "<-loopback>,localhost,*.localhost,127.0.0.1/8,[::1]"
"""Proxy bypass rules for loopback hosts and nothing else; Chrome's implicit ones would also bypass link-local addresses."""

_NO_BYPASS = "<-loopback>"
"""Proxy bypass rules that send loopback hosts to the proxy too, which Chrome would otherwise reach directly."""

_WEBRTC_PROXIED_ONLY = "--webrtc-ip-handling-policy=disable_non_proxied_udp"
"""Keeps WebRTC off UDP, which no proxy carries, so that it goes through the proxy, over TCP, or nowhere."""

logger = logging.getLogger(__name__)

_EXTRA_HINT = "rendering needs patchright; install it with: pip install 'pernix[browser]'"
_ENGINE_HINT = "the browser engine is not installed; install it with: pernix browser install"


@dataclass(slots=True)
class LaunchedBrowser:
    """A running browser and the patchright driver that runs it.

    Attributes:
        playwright: The patchright driver.
        browser: The browser.
        guarded: Whether it was launched under the network guard, so that only
            loopback hosts are reached without a proxy of the Request's own.
        refusing_proxy: Where a guarded browser sends everything else.
        devtools: A session of the browser's DevTools protocol, which `probe()`
            goes through; `None` if the browser has none.
        processes: The processes of the browser and of its driver; `None` if they
            cannot be found, as without psutil.
        guard_proxy: Where a browser launched while `network.public_only` is on
            sends all its traffic, the proxy that checks every host it reaches.
    """

    playwright: Playwright
    browser: Browser
    guarded: bool
    refusing_proxy: RefusingProxy | None
    devtools: CDPSession | None = None
    processes: BrowserProcesses | None = None
    guard_proxy: AddressGuardProxy | None = None

    async def new_context(
        self,
        *,
        proxy: str | None = None,
        local_storage: Mapping[str, Mapping[str, str]] | None = None,
        blocks_service_workers: bool = False,
    ) -> BrowserContext:
        """Create a context, for one fetch or for the fetches of a Session.

        Args:
            proxy: The proxy URL every request of the context goes through, loopback
                ones included; without one, the browser's own applies, as the
                proxy that checks hosts for `network.public_only` does.
            local_storage: The localStorage the context starts with, by origin, then
                by key.
            blocks_service_workers: Keep its pages from registering service
                workers, whose requests no route of a page sees; registering one
                then never completes.

        Returns:
            The context, without pages.
        """
        options: dict[str, Any] = {}
        if proxy:
            options["proxy"] = _proxy_settings(proxy)
        if local_storage:
            options["storage_state"] = _storage_state(local_storage)
        if blocks_service_workers:
            options["service_workers"] = "block"
        return await self.browser.new_context(**options)

    async def probe(self) -> None:
        """Ask the browser for its version, through the driver, which fails, or never ends, once either went away.

        Raises:
            Exception: The browser or the driver did not answer.
        """
        if self.devtools is None:  # a browser without DevTools: a round trip of another kind
            context = await self.browser.new_context()
            await context.close()
            return
        await _send(self.devtools, "Browser.getVersion")

    async def close(self) -> None:
        """Close the browser, stop the driver, and stop the proxy it was launched with."""
        try:
            await self.browser.close()
        finally:
            try:
                await self.playwright.stop()
            finally:
                await _close_proxies(self.refusing_proxy, self.guard_proxy)


async def launch_browser(settings: BrowserSettings, *, address_guard: AddressGuard | None = None) -> LaunchedBrowser:
    """Start patchright and launch a browser.

    The browser is Chrome for Testing, in the new headless mode unless
    `browser.headless` is off, with `browser.launch_options` over those defaults.
    With an address guard, as `network.public_only` asks, all its traffic goes
    through a proxy that checks every host (`pernix.fetchers.address_guard_proxy`).
    Under the network guard, every connection to a host other than a loopback one
    goes to a proxy that refuses it, whatever `launch_options` say. Under either,
    WebRTC is kept off UDP, which would go around the proxy.

    Args:
        settings: The browser settings.
        address_guard: The address guard that checks the hosts the browser reaches,
            if any.

    Returns:
        The running browser.

    Raises:
        pernix.errors.ComponentError: patchright or its browser engine is not
            installed, or the executable `launch_options` name does not exist.
    """
    try:
        from patchright.async_api import Error, async_playwright  # an optional dependency, imported when needed
    except ImportError as exc:
        raise ComponentError(_EXTRA_HINT) from exc
    options: dict[str, Any] = {"channel": "chromium", **settings.launch_options, "headless": settings.headless}
    network_guard = current_guard()
    refusing_proxy: RefusingProxy | None = None
    guard_proxy: AddressGuardProxy | None = None
    playwright: Playwright | None = None
    try:
        if address_guard is not None:
            guard_proxy = AddressGuardProxy(address_guard, network_guard=network_guard)
            await guard_proxy.start()
            options["proxy"] = {"server": guard_proxy.url, "bypass": _NO_BYPASS}
        elif network_guard is not None:
            refusing_proxy = RefusingProxy()
            await refusing_proxy.start()
            options["proxy"] = {"server": refusing_proxy.url, "bypass": LOOPBACK_BYPASS}
        if address_guard is not None or network_guard is not None:
            options["args"] = [*options.get("args", ()), _WEBRTC_PROXIED_ONLY]
        playwright = await _start(async_playwright())
        try:
            browser = await playwright.chromium.launch(**options)
        except Error as exc:
            if "executable doesn't exist" in str(exc).lower():
                raise ComponentError(_missing_executable(settings)) from exc
            raise
        devtools, processes = await _inspect(browser)
    except BaseException:
        if playwright is not None:
            await playwright.stop()
        await _close_proxies(refusing_proxy, guard_proxy)
        raise
    guarded = network_guard is not None
    return LaunchedBrowser(playwright, browser, guarded, refusing_proxy, devtools, processes, guard_proxy)


async def probe_browser(settings: BrowserSettings) -> str:
    """Launch a browser as a fetch would and close it, to tell whether it starts.

    Args:
        settings: The browser settings.

    Returns:
        The browser's name and version, such as `chromium 151.0.7922.34`.

    Raises:
        pernix.errors.ComponentError: patchright or its browser engine is not
            installed, or the browser does not start; the message says why.
    """
    try:
        launched = await launch_browser(settings)
    except ComponentError:
        raise
    except Exception as exc:  # patchright's own errors, such as for system libraries that are missing
        raise ComponentError(f"the browser does not start: {exc}") from exc
    try:
        return f"{launched.browser.browser_type.name} {launched.browser.version}"
    finally:
        await launched.close()


async def _inspect(browser: Browser) -> tuple[CDPSession | None, BrowserProcesses | None]:
    """A DevTools session of a browser just launched, and its processes, as far as it tells them."""
    try:
        devtools = await browser.new_browser_cdp_session()
    except Exception:  # a browser without DevTools
        logger.debug("The browser has no DevTools session", exc_info=True)
        return None, None
    try:
        info = await _send(devtools, "SystemInfo.getProcessInfo")
    except Exception:
        logger.debug("The browser does not tell its processes", exc_info=True)
        return devtools, None
    processes = cast("list[dict[str, Any]]", info.get("processInfo", []))
    pid = next((int(process["id"]) for process in processes if process.get("type") == "browser"), None)
    return devtools, None if pid is None else BrowserProcesses.of(pid)


async def _send(devtools: CDPSession, method: str) -> dict[str, Any]:
    """Send a command of the DevTools protocol, and return its result."""
    return cast("dict[str, Any]", await devtools.send(method))  # pyright: ignore[reportUnknownMemberType]  # untyped


def require_patchright() -> None:
    """Make sure patchright, which the `browser` extra installs, is installed.

    Raises:
        pernix.errors.ComponentError: It is not; the message says how to install it.
    """
    if importlib.util.find_spec("patchright") is None:
        raise ComponentError(_EXTRA_HINT)


def _missing_executable(settings: BrowserSettings) -> str:
    """What to do about a browser executable that does not exist: install the engine, or name another one."""
    path = settings.launch_options.get("executable_path")
    if path is None:
        return _ENGINE_HINT
    return f"the browser executable {path} does not exist; change {settings.key('launch_options')}"


async def _start(manager: PlaywrightContextManager) -> Playwright:
    """Start patchright's driver, finishing a start that is cancelled before stopping it.

    patchright cannot stop a driver it is still starting: cancelled midway, it would
    leave the driver and a task reading from it running, which keeps the event loop
    from closing.
    """
    starting = asyncio.ensure_future(manager.start())
    try:
        return await asyncio.shield(starting)
    except asyncio.CancelledError:
        await asyncio.wait([starting])
        if not starting.cancelled() and starting.exception() is None:
            await starting.result().stop()
        raise


async def _close_proxies(refusing_proxy: RefusingProxy | None, guard_proxy: AddressGuardProxy | None) -> None:
    """Stop the proxies a browser was launched with, if any."""
    try:
        if refusing_proxy is not None:
            await refusing_proxy.close()
    finally:
        if guard_proxy is not None:
            await guard_proxy.close()


def _storage_state(local_storage: Mapping[str, Mapping[str, str]]) -> StorageState:
    """The storage state patchright starts a context with: no cookies, and localStorage by origin."""
    return {
        "cookies": [],
        "origins": [
            {"origin": origin, "localStorage": [{"name": name, "value": value} for name, value in items.items()]}
            for origin, items in local_storage.items()
        ],
    }


def _proxy_settings(proxy: str) -> ProxySettings:
    """A proxy URL as patchright takes it: the server apart from the credentials."""
    parts = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    settings: ProxySettings = {"server": f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"}
    if parts.username is not None:
        settings["username"] = unquote(parts.username)
    if parts.password is not None:
        settings["password"] = unquote(parts.password)
    return settings
