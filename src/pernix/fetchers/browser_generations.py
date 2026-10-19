"""The browsers of a browser Fetcher, one Generation after another: replaced as they age, and restarted once lost."""

from __future__ import annotations

import asyncio
import importlib.util
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from pernix.errors import BrowserLost, ComponentError, PageCrashed
from pernix.fetchers.launched_browser import launch_browser
from pernix.fetchers.tripwire import Tripwire

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pernix.fetchers.browser_settings import BrowserSettings
    from pernix.fetchers.launched_browser import LaunchedBrowser
    from pernix.stats import Stats

__all__ = ["Generation", "Generations"]

logger = logging.getLogger(__name__)

PROBE_INTERVAL = 10.0
"""Seconds between two health probes of a browser, which also measure its memory."""

PROBE_TIMEOUT = 10.0
"""Seconds a health probe may take before the browser is taken to have stopped answering."""

CLOSE_TIMEOUT = 10.0
"""Seconds closing a browser and its driver may take before their processes are killed."""

_MIB = 1024 * 1024

_LOSSES = {
    "disconnected": "the browser disconnected",
    "driver": "patchright's driver died",
    "unresponsive": "the browser stopped answering",
}
"""What each reason a Generation is lost for tells."""

_CLOSED_MARKERS = (
    "Target page, context or browser has been closed",
    "Target crashed",
    "Page crashed",
    "Browser has been closed",
    "Connection closed while reading from the driver",
)
"""Parts of the messages of patchright's errors for a page, a browser, or a driver that went away."""

_DRIVER_GONE = "Connection closed while reading from the driver"


class Generation:
    """One browser and the patchright driver running it, from its launch until it is closed.

    It takes the new pages until it is replaced, as it grew too old, opened too many
    pages, or took too much memory, or until it is lost, as it disconnected, its
    driver died, or it stopped answering. A lost Generation trips its `tripwire`,
    ending the fetches in it with `BrowserLost`. Either way it is closed once its
    last fetch ends.

    Attributes:
        launched: The browser and its driver.
        started: When it was launched, on the monotonic clock.
        pages: The fetches that opened a page in it.
        fetching: The fetches in it.
        replaced: Why it was replaced, `age`, `pages`, or `memory`, if it was.
        tripwire: Tripped with the reason it was lost, if it was.
        closing: Whether it is being closed, or was.
        probing: The task probing its health and memory, while it runs.
    """

    def __init__(self, launched: LaunchedBrowser, started: float) -> None:
        """Track a browser just launched.

        Args:
            launched: The browser.
            started: When it was launched, on the monotonic clock.
        """
        self.launched = launched
        self.started = started
        self.pages = 0
        self.fetching = 0
        self.replaced: str | None = None
        self.tripwire = Tripwire()
        self.closing = False
        self.probing: asyncio.Task[None] | None = None

    @property
    def lost(self) -> str | None:
        """Why it was lost, `disconnected`, `driver`, or `unresponsive`, if it was."""
        return self.tripwire.reason

    def lost_error(self, url: str) -> BrowserLost | None:
        """The error a fetch in it fails with once it is lost; `None` while it is not.

        Args:
            url: The URL of the fetch, for the message.

        Returns:
            The `BrowserLost`, naming why it was lost, or `None`.
        """
        reason = self.lost
        return None if reason is None else BrowserLost(f"rendering {url} failed: {_LOSSES[reason]}")

    @property
    def taking_pages(self) -> bool:
        """Whether new fetches may use it: it was neither replaced, lost, nor closed."""
        return self.replaced is None and self.lost is None and not self.closing


class Generations:
    """The browsers of a browser Fetcher: the current Generation, which takes the new fetches, and those ending.

    The first fetch that needs a browser launches one while the others wait, and so
    does the first after the current one was replaced or lost, so one browser starts
    at a time. A Generation is replaced once it is `browser.generation_age` seconds
    old or opened `browser.generation_pages` pages, as the next fetch finds, or once
    its processes take more than `browser.generation_memory` MiB, as its health probe
    finds; 0 turns each off. The fetches in it finish there while the new ones go to
    the next, and it is closed once the last one ends.

    A Generation is lost when its browser disconnects, as when its process died; when
    its driver died, as an error of a fetch, or a probe, tells; or when it stopped
    answering a health probe, every `PROBE_INTERVAL` seconds, within
    `PROBE_TIMEOUT`, in which case its driver is killed. The fetches in it end with
    `BrowserLost`, and the next fetch launches another. A driver that dies leaves its
    browser running, so the processes of every Generation are killed once it closed.

    In `stats`, if given, `browser.generations` counts the browsers launched,
    `browser.replaced` and `browser.replaced_by_reason.<reason>` those replaced, and
    `browser.lost` and `browser.lost_by_reason.<reason>` those lost.
    """

    def __init__(
        self,
        settings: BrowserSettings,
        *,
        retire: Callable[[LaunchedBrowser], object],
        stats: Stats | None = None,
        launch: Callable[[BrowserSettings], Awaitable[LaunchedBrowser]] = launch_browser,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Configure the Generations.

        Args:
            settings: The browser settings.
            retire: Told of a browser that takes no new fetches, so that the
                contexts kept for Sessions in it are closed.
            stats: Where the browsers launched, replaced, and lost are counted.
            launch: Launches a browser.
            clock: A monotonic clock, in seconds.
        """
        self._settings = settings
        self._retire = retire
        self._stats = stats
        self._launch_browser = launch
        self._clock = clock
        self._current: Generation | None = None
        self._live: set[Generation] = set()
        self._launch: asyncio.Task[Generation] | None = None
        self._unavailable: ComponentError | None = None
        self._closing: set[asyncio.Task[None]] = set()

    def open(self) -> None:
        """Let fetches launch a browser again, as after the fetcher is entered anew."""
        self._unavailable = None

    @asynccontextmanager
    async def fetching(self, url: str) -> AsyncGenerator[Generation]:
        """Use the current browser for one fetch, launching one if there is none.

        Args:
            url: The URL of the fetch, for errors.

        Yields:
            The Generation, whose browser is not closed meanwhile.

        Raises:
            pernix.errors.BrowserLost: The browser was lost before the fetch was
                done, which is then cancelled.
            pernix.errors.ComponentError: patchright, its browser engine, or psutil
                for `browser.generation_memory`, is not installed.
        """
        generation = await self._taking_pages()
        generation.pages += 1
        generation.fetching += 1
        try:
            async with generation.tripwire.guarding(
                lambda reason: BrowserLost(f"rendering {url} failed: {_LOSSES[reason]}"),
                may_trip=lambda error: self.may_be_lost(generation, error),
            ):
                yield generation
        finally:
            generation.fetching -= 1
            self._close_if_done(generation)

    def keep(self, generation: Generation) -> Callable[[], None]:
        """Count a page kept open past its fetch as a fetch in its Generation, which is then not closed under it.

        Args:
            generation: The Generation the page is open in.

        Returns:
            Ends the count, once the page is closed; calling it again does nothing.
        """
        generation.fetching += 1
        released = False

        def release() -> None:
            nonlocal released
            if not released:
                released = True
                generation.fetching -= 1
                self._close_if_done(generation)

        return release

    async def close(self) -> None:
        """Stop a launch in progress, then close every browser, waiting for them."""
        launch, self._launch = self._launch, None
        if launch is not None:
            if not launch.done():
                launch.cancel()
            await asyncio.wait([launch])
        for generation in list(self._live):
            self._close(generation)
        self._current = None
        if self._closing:
            await asyncio.wait(self._closing)

    async def _taking_pages(self) -> Generation:
        """The Generation new fetches use: the current one, unless it must be replaced, else one launched for them.

        A fetch that waited for a launch checks the Generation launched as any other
        does, as the fetches that waited with it may have opened its pages, or it may
        have been lost meanwhile.
        """
        while True:
            if self._unavailable is not None:
                raise ComponentError(*self._unavailable.args) from self._unavailable.__cause__
            current = self._current
            if current is not None and current.taking_pages:
                reason = self._replace_reason(current)
                if reason is None:
                    return current
                self._replace(current, reason)
            if self._launch is None:
                self._launch = asyncio.create_task(self._start())
            try:
                await asyncio.shield(self._launch)  # a fetch that gives up waiting leaves the launch to the others
            except ComponentError as exc:
                self._unavailable = exc  # installing the extra or the engine takes a new Run
                raise

    async def _start(self) -> Generation:
        """Launch a browser and make it the current Generation; the next fetch to find none launches another."""
        try:
            return await self._started()
        finally:
            if self._launch is asyncio.current_task():
                self._launch = None

    async def _started(self) -> Generation:
        if self._settings.generation_memory and importlib.util.find_spec("psutil") is None:
            raise ComponentError(
                f"{self._settings.key('generation_memory')} needs psutil to measure the browser's memory; "
                "install it with: pip install 'pernix[browser]'"
            )
        launched = await self._launch_browser(self._settings)
        generation = Generation(launched, self._clock())
        self._live.add(generation)
        self._current = generation
        launched.browser.on("disconnected", lambda _browser: self._lose(generation, "disconnected"))
        generation.probing = asyncio.create_task(self._probe(generation))
        if self._stats is not None:
            self._stats.inc("browser.generations")
        if self._settings.generation_memory and launched.processes is None:
            logger.warning(
                "The processes of the browser cannot be found, so its memory is not measured for %s",
                self._settings.key("generation_memory"),
            )
        return generation

    def _replace_reason(self, generation: Generation) -> str | None:
        """Why a Generation must be replaced before it takes another fetch, if it must."""
        settings = self._settings
        if settings.generation_age and self._clock() - generation.started >= settings.generation_age:
            return "age"
        if settings.generation_pages and generation.pages >= settings.generation_pages:
            return "pages"
        return None

    def _replace(self, generation: Generation, reason: str) -> None:
        """Stop giving a Generation new fetches; it closes once the fetches in it end."""
        if not generation.taking_pages:
            return
        generation.replaced = reason
        if self._current is generation:
            self._current = None
        logger.info("Replacing the browser (%s), once its %d fetches end", reason, generation.fetching)
        if self._stats is not None:
            self._stats.inc("browser.replaced")
            self._stats.inc(f"browser.replaced_by_reason.{reason}")
        self._retire(generation.launched)
        self._close_if_done(generation)

    def _lose(self, generation: Generation, reason: str) -> None:
        """End the fetches in a Generation that went away with `BrowserLost`, and close it."""
        if generation.closing or generation.lost is not None:
            return  # a browser closing disconnects too
        if self._current is generation:
            self._current = None
        logger.warning("%s; the fetches in it fail with BrowserLost, and the next starts another", _LOSSES[reason])
        if self._stats is not None:
            self._stats.inc("browser.lost")
            self._stats.inc(f"browser.lost_by_reason.{reason}")
        processes = generation.launched.processes
        if reason == "unresponsive" and processes is not None:
            processes.kill_driver()  # so that what waits for it fails
        generation.tripwire.trip(reason)
        self._retire(generation.launched)
        self._close_if_done(generation)

    def may_be_lost(self, generation: Generation, error: BaseException) -> bool:
        """Tell whether a failure in a Generation may be its going away, losing the Generation if it plainly is.

        Args:
            generation: The Generation the failure happened in.
            error: The failure, such as the error of a fetch, or of a Callback
                working with a page kept in the Generation.

        Returns:
            Whether the error names a page, a browser, or a driver that went away,
            so that the event that tells which may still come; the Generation is lost
            at once when the error tells its driver died, or its browser is no
            longer connected.
        """
        processes = generation.launched.processes
        if _driver_gone(error) or (processes is not None and not processes.driver_alive()):
            self._lose(generation, "driver")
        elif not generation.launched.browser.is_connected():
            self._lose(generation, "disconnected")
        return not isinstance(error, PageCrashed) and _browser_closed(error)

    async def _probe(self, generation: Generation) -> None:
        """Probe a Generation's health, and its memory, periodically, until it is lost or closed."""
        limit = self._settings.generation_memory * _MIB
        processes = generation.launched.processes
        while generation.lost is None:
            await asyncio.sleep(PROBE_INTERVAL)
            # Cancelled, a call to a driver that does not answer waits for it all the same, so it is left to finish.
            probe = _unattended(generation.launched.probe())
            done, _ = await asyncio.wait([probe], timeout=PROBE_TIMEOUT)
            if not done:
                self._lose(generation, "unresponsive")
                return
            if probe.exception() is not None:
                logger.debug("The health probe of the browser failed", exc_info=probe.exception())
                self._lose(generation, self._loss_reason(generation))
                return
            if processes is None:
                continue
            await asyncio.to_thread(processes.remember)  # those it started, in case it ends before they do
            if limit and await asyncio.to_thread(processes.memory) > limit:
                self._replace(generation, "memory")

    def _loss_reason(self, generation: Generation) -> str:
        """Why a Generation whose probe failed is lost."""
        processes = generation.launched.processes
        if processes is not None and not processes.driver_alive():
            return "driver"
        return "disconnected"

    def _close_if_done(self, generation: Generation) -> None:
        """Close a Generation that takes no new fetches once the last fetch in it ended."""
        if not generation.taking_pages and generation.fetching == 0:
            self._close(generation)

    def _close(self, generation: Generation) -> None:
        """Start closing a Generation in a task of its own, once."""
        if generation.closing:
            return
        generation.closing = True
        if generation.probing is not None:
            generation.probing.cancel()
        closing = asyncio.create_task(self._closed(generation))
        self._closing.add(closing)
        closing.add_done_callback(self._closing.discard)

    async def _closed(self, generation: Generation) -> None:
        """Close a Generation's browser and driver, then kill what is left of its processes.

        A driver that does not close within `CLOSE_TIMEOUT` is killed, so that the
        close, which waits for it even when cancelled, ends.
        """
        processes = generation.launched.processes
        closing = _unattended(generation.launched.close())
        try:
            done, _ = await asyncio.wait([closing], timeout=CLOSE_TIMEOUT)
            if not done and processes is not None:
                processes.kill_driver()
                await asyncio.wait([closing], timeout=CLOSE_TIMEOUT)
            if closing.done() and closing.exception() is not None:  # a browser lost, or a driver dead, fails to close
                logger.debug("Closing a browser failed; killing its processes", exc_info=closing.exception())
        finally:
            self._live.discard(generation)
            if processes is not None:
                await asyncio.to_thread(processes.kill)  # a driver that died left its browser running


def _unattended(work: Awaitable[None]) -> asyncio.Future[None]:
    """Run work in a task of its own whose failure, if no one asks for it, is not reported as never retrieved."""
    future = asyncio.ensure_future(work)
    future.add_done_callback(lambda done: done.cancelled() or done.exception())
    return future


def _browser_closed(error: BaseException) -> bool:
    """Whether an error, or one that caused it, tells of a page, a browser, or a driver that went away."""
    return any(marker in str(cause) for cause in _causes(error) for marker in _CLOSED_MARKERS)


def _driver_gone(error: BaseException) -> bool:
    """Whether an error, or one that caused it, tells of patchright's driver having died."""
    return any(_DRIVER_GONE in str(cause) for cause in _causes(error))


def _causes(error: BaseException) -> list[BaseException]:
    """An error and those that caused it, nearest first."""
    causes: list[BaseException] = []
    cause: BaseException | None = error
    while cause is not None and cause not in causes:
        causes.append(cause)
        cause = cause.__cause__ or cause.__context__
    return causes
