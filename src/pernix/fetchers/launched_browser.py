"""Starting patchright and its browser for the browser Fetcher, under the network guard if it is on, and probing it."""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import unquote, urlsplit

from pernix.errors import ComponentError
from pernix.fetchers.address_guard_proxy import AddressGuardProxy
from pernix.fetchers.browser_processes import BrowserProcesses
from pernix.fetchers.network_guard import current_guard
from pernix.fetchers.page_intl import present_intl
from pernix.fetchers.pinned_pair import BUNDLED_BROWSER
from pernix.fetchers.refusing_proxy import RefusingProxy
from pernix.settings import DEFAULT_LOCALE

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from patchright._impl._api_structures import StorageState
    from patchright.async_api import Browser, BrowserContext, CDPSession, Page, Playwright, ProxySettings
    from patchright.async_api._context_manager import PlaywrightContextManager

    from pernix.fetchers.address_guard import AddressGuard
    from pernix.fetchers.browser_settings import BrowserSettings, Preset

__all__ = [
    "LOOPBACK_BYPASS",
    "LaunchedBrowser",
    "launch_browser",
    "presented_languages",
    "probe_browser",
    "report_unpinned",
    "require_patchright",
]

LOOPBACK_BYPASS = "<-loopback>,localhost,*.localhost,127.0.0.1/8,[::1]"
"""Proxy bypass rules for loopback hosts and nothing else; Chrome's implicit ones would also bypass link-local addresses."""

_NO_BYPASS = "<-loopback>"
"""Proxy bypass rules that send loopback hosts to the proxy too, which Chrome would otherwise reach directly."""

_BYPASS_ALL = "*"
"""Proxy bypass rules that bypass every host, so that a context with them reaches each directly."""

_WEBRTC_PROXIED_ONLY = "--webrtc-ip-handling-policy=disable_non_proxied_udp"
"""Keeps WebRTC off UDP, which no proxy carries, so that it goes through the proxy, over TCP, or nowhere."""

logger = logging.getLogger(__name__)

_HEADLESS = "HeadlessChrome/"
"""What names the browser in its User-Agent in headless mode, and `Chrome/` with a window."""

_EXTRA_HINT = "rendering needs patchright; install it with: pip install 'pernix[browser]'"
_ENGINE_HINT = "the browser engine is not installed; install it with: pernix browser install"
_DISPLAY_HINT = (
    "the browser runs with a window, which needs a display; run it under a virtual one, such as with xvfb-run, "
    "or set browser.headless = true"
)


@dataclass(slots=True)
class LaunchedBrowser:
    """A running browser and the patchright driver that runs it.

    Attributes:
        playwright: The patchright driver.
        browser: The browser.
        guarded: Whether it was launched under the network guard, so that only
            loopback hosts are reached without a proxy of the Request's own.
        refusing_proxy: The proxy the browser was launched with, which refuses
            what it is sent: the browser's own requests, which belong to no
            context, and, under the network guard, everything but loopback
            traffic; `None` for a proxy `browser.launch_options` set.
        devtools: A session of the browser's DevTools protocol, which `probe()`
            goes through; `None` if the browser has none.
        processes: The processes of the browser and of its driver; `None` if they
            cannot be found, as without psutil.
        user_agent: The User-Agent its contexts present: its own, without
            `HeadlessChrome`, in headless mode; `None` for its own.
        guard_proxy: Where the contexts of a browser launched while
            `network.public_only` is on send all their traffic, the proxy that
            checks every host they reach.
        context_proxy: The proxy of the contexts without one of their own: the
            guard proxy, or none, bypassing every host, so that the launch proxy
            takes only the browser's own requests; `None` to leave them the
            launch proxy, as under the network guard alone.
        timezone: The time zone its contexts present, an IANA name; `None` for the
            machine's.
        intl_locale: The locale of `Intl` its pages present, with their frames and
            web workers; `None` for the machine's.
        preset: How it was launched, `browser.preset`: its contexts' pages have
            no viewport of their own under `stealth`, so that they get the
            window's.
    """

    playwright: Playwright
    browser: Browser
    guarded: bool
    refusing_proxy: RefusingProxy | None
    devtools: CDPSession | None = None
    processes: BrowserProcesses | None = None
    user_agent: str | None = None
    guard_proxy: AddressGuardProxy | None = None
    context_proxy: ProxySettings | None = None
    timezone: str | None = None
    intl_locale: str | None = None
    preset: Preset = "bundled"

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
                ones included; without one, `context_proxy`, if set, else the one
                the browser was launched with.
            local_storage: The localStorage the context starts with, by origin, then
                by key.
            blocks_service_workers: Keep its pages from registering service
                workers, whose requests no route of a page sees; registering one
                then never completes.

        Returns:
            The context, without pages, presenting `user_agent` and `timezone`, if
            set: patchright's `timezone_id` sets the time zone of every page, frame,
            and web worker, without moving a header. Under the `stealth` preset, its
            pages have no viewport of their own.
        """
        options: dict[str, Any] = {}
        if self.preset == "stealth":
            options["no_viewport"] = True
        if self.user_agent is not None:
            options["user_agent"] = self.user_agent
        if self.timezone is not None:
            options["timezone_id"] = self.timezone
        if proxy:
            options["proxy"] = _proxy_settings(proxy)
        elif self.context_proxy is not None:
            options["proxy"] = self.context_proxy
        if local_storage:
            options["storage_state"] = _storage_state(local_storage)
        if blocks_service_workers:
            options["service_workers"] = "block"
        return await self.browser.new_context(**options)

    async def new_page(self, context: BrowserContext) -> Page:
        """Open a page in a context, presenting `intl_locale` and `timezone`, if set, before it loads anything.

        Args:
            context: A context of the browser, from `new_context()`.

        Returns:
            The page, whose frames and web workers present them too
            (`pernix.fetchers.page_intl`).
        """
        page = await context.new_page()
        if self.intl_locale is None and self.timezone is None:
            return page
        try:
            await present_intl(context, page, locale=self.intl_locale, timezone=self.timezone)
        except BaseException:
            with contextlib.suppress(Exception):  # a page that went away
                await page.close()
            raise
        return page

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


async def launch_browser(
    settings: BrowserSettings,
    *,
    locale: Sequence[str] = DEFAULT_LOCALE,
    timezone: str | None = None,
    address_guard: AddressGuard | None = None,
    proxy_pool: bool = False,
) -> LaunchedBrowser:
    """Start patchright and launch a browser.

    The browser is Chrome for Testing, in the new headless mode, or, under the
    `stealth` preset, the system's own Chrome with a window, as `browser.headless`
    may change, with `browser.launch_options` over those defaults.
    It presents the languages of `locale`, through Chrome's `--accept-lang`, unless
    the `args` of `launch_options` set it, and the first of them as the locale of
    `Intl`; its contexts present its User-Agent without `HeadlessChrome`, as it is
    with a window, and `timezone`, if given.

    It is launched with a proxy that refuses what it is sent, unless
    `launch_options` set one, so that its own requests, which belong to no context
    and so ignore the contexts' proxies, such as those checking the network time
    or its Google accounts, leave nothing from the machine: its contexts are
    given a proxy each, their own, or, without one, none, bypassing every host.
    With an address guard, as `network.public_only` asks, the contexts without a
    proxy of their own go through a proxy that checks every host
    (`pernix.fetchers.address_guard_proxy`) instead. Under the network guard,
    whatever `launch_options` say, they go through the refusing proxy, which
    refuses every host but loopback ones. Under either guard, and with a Proxy Pool,
    WebRTC is kept off UDP, which would go around the proxies, telling pages the
    machine's own address.

    Args:
        settings: The browser settings.
        locale: The languages to present, in order.
        timezone: The time zone to present, an IANA name; `None` for the machine's.
        address_guard: The address guard that checks the hosts the browser reaches,
            if any.
        proxy_pool: Whether the Run goes through a Proxy Pool.

    Returns:
        The running browser.

    Raises:
        pernix.errors.ComponentError: patchright or its browser engine is not
            installed, the browser the preset or `launch_options` names is not,
            or one with a window has no display.
    """
    try:
        from patchright.async_api import Error, async_playwright  # an optional dependency, imported when needed
    except ImportError as exc:
        raise ComponentError(_EXTRA_HINT) from exc
    args = [str(arg) for arg in settings.launch_options.get("args") or ()]  # patchright takes None for none
    if not any(arg.startswith("--accept-lang=") for arg in args):
        args.append(f"--accept-lang={','.join(locale)}")
    options: dict[str, Any] = {
        "channel": "chrome" if settings.preset == "stealth" else "chromium",
        **settings.launch_options,
        "args": args,
        "headless": settings.runs_headless,
    }
    network_guard = current_guard()
    refusing_proxy: RefusingProxy | None = None
    guard_proxy: AddressGuardProxy | None = None
    context_proxy: ProxySettings | None = None
    playwright: Playwright | None = None
    try:
        if network_guard is not None or "proxy" not in options:
            refusing_proxy = RefusingProxy()
            await refusing_proxy.start()
            bypass = LOOPBACK_BYPASS if network_guard is not None else _NO_BYPASS
            options["proxy"] = {"server": refusing_proxy.url, "bypass": bypass}
            if network_guard is None:
                context_proxy = {"server": refusing_proxy.url, "bypass": _BYPASS_ALL}
        if address_guard is not None:
            guard_proxy = AddressGuardProxy(address_guard, network_guard=network_guard)
            await guard_proxy.start()
            context_proxy = {"server": guard_proxy.url, "bypass": _NO_BYPASS}
        if address_guard is not None or network_guard is not None or proxy_pool:
            options["args"] = [*options["args"], _WEBRTC_PROXIED_ONLY]
        playwright = await _start(async_playwright())
        try:
            browser = await playwright.chromium.launch(**options)
        except Error as exc:
            problem = _launch_problem(str(exc), settings, channel=str(options["channel"]))
            if problem is not None:
                raise ComponentError(problem) from exc
            raise
        devtools, processes = await _inspect(browser)
        user_agent = await _headed_user_agent(devtools)
    except BaseException:
        if playwright is not None:
            await playwright.stop()
        await _close_proxies(refusing_proxy, guard_proxy)
        raise
    guarded = network_guard is not None
    return LaunchedBrowser(
        playwright,
        browser,
        guarded,
        refusing_proxy,
        devtools,
        processes,
        user_agent,
        guard_proxy,
        context_proxy,
        timezone=timezone,
        intl_locale=locale[0],
        preset=settings.preset,
    )


async def probe_browser(settings: BrowserSettings, *, locale: Sequence[str] = DEFAULT_LOCALE) -> str:
    """Launch a browser as a fetch would and close it, to tell whether it starts.

    A browser other than the bundled build, or presenting other languages than
    `locale`, is warned about, as `report_unpinned()` does.

    Args:
        settings: The browser settings.
        locale: The languages to present, in order.

    Returns:
        The browser's name and version, and whether it is the bundled build, such
        as `chromium 151.0.7922.34 (bundled)`, or the stealth preset's.

    Raises:
        pernix.errors.ComponentError: patchright or its browser engine is not
            installed, or the browser does not start; the message says why.
    """
    try:
        launched = await launch_browser(settings, locale=locale)
    except ComponentError:
        raise
    except Exception as exc:  # patchright's own errors, such as for system libraries that are missing
        raise ComponentError(f"the browser does not start: {exc}") from exc
    try:
        await report_unpinned(launched, locale)
        version = launched.browser.version
        if version == BUNDLED_BROWSER.version:
            pinned = "bundled"
        elif settings.preset == "stealth":
            pinned = f"stealth preset; the HTTP Fetcher presents the bundled {BUNDLED_BROWSER.version}"
        else:
            pinned = f"not the bundled {BUNDLED_BROWSER.version}"
        return f"{launched.browser.browser_type.name} {version} ({pinned})"
    finally:
        await launched.close()


async def report_unpinned(launched: LaunchedBrowser, locale: Sequence[str]) -> None:
    """Warn about a browser that presents another client than the HTTP Fetcher does with the pinned Profile.

    That is a browser other than the bundled build, or one presenting other
    languages than `locale`, as Chrome does for some lists, which it replaces.
    Under the `stealth` preset, which launches another browser on purpose, the
    first is told at INFO instead.

    Args:
        launched: The browser.
        locale: The languages it was launched to present.
    """
    version = launched.browser.version
    if version != BUNDLED_BROWSER.version and launched.preset == "stealth":
        logger.info(
            "The stealth preset launched the browser %s, while the HTTP Fetcher presents the bundled build %s with "
            "the pinned Profile, so the Sessions both use present two clients",
            version,
            BUNDLED_BROWSER.version,
        )
    elif version != BUNDLED_BROWSER.version:
        logger.warning(
            "The browser is %s, not the bundled build %s, which the HTTP Fetcher presents with the pinned Profile, "
            "so the two present different clients",
            version,
            BUNDLED_BROWSER.version,
        )
    languages = await presented_languages(launched)
    if languages != tuple(locale):
        logger.warning(
            "The browser presents the languages %s for the locale %s, so the HTTP Fetcher presents others; "
            "set locale to %r for both to present them",
            ", ".join(languages),
            ", ".join(locale),
            ",".join(languages),
        )


async def presented_languages(launched: LaunchedBrowser) -> tuple[str, ...]:
    """Ask a browser which languages its pages present, as `navigator.languages`.

    Args:
        launched: The browser.

    Returns:
        The languages, in order.
    """
    context = await launched.browser.new_context()
    try:
        page = await context.new_page()
        languages = cast("list[str]", await page.evaluate("navigator.languages"))
    finally:
        await context.close()
    return tuple(languages)


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


async def _headed_user_agent(devtools: CDPSession | None) -> str | None:
    """The User-Agent a browser in headless mode would have with a window; `None` with one, or without DevTools."""
    if devtools is None:
        return None
    user_agent = str((await _send(devtools, "Browser.getVersion")).get("userAgent", ""))
    return user_agent.replace(_HEADLESS, "Chrome/") if _HEADLESS in user_agent else None


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


def _launch_problem(message: str, settings: BrowserSettings, *, channel: str) -> str | None:
    """What to do about a browser that did not launch for want of something the user can install; `None` otherwise."""
    if "executable doesn't exist" in message.lower():
        return _missing_executable(settings)
    if "is not found at" in message:  # a Chrome of the system, which patchright does not install by itself
        found = message.splitlines()[0].split(": ", 1)[-1]
        return f"{found}; install it, such as with: patchright install {channel}"
    if "XServer" in message or "$DISPLAY" in message:
        return _DISPLAY_HINT
    return None


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
