"""The proxies of a Run: loaded from its sources into its Proxy Pool, refreshed, and given to its fetches."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

from pernix.errors import ProxyError
from pernix.proxies.proxy_url import proxy_credentials, proxy_url
from pernix.sending import NotYet

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable, Sequence

    from pernix.proxies.proxy_pool import ProxyPool
    from pernix.proxies.proxy_sources import ProxyProvider
    from pernix.sending import Sending
    from pernix.stats import Stats

__all__ = ["NoProxyYet", "RunProxies"]

logger = logging.getLogger(__name__)

_EXHAUSTED_WAIT = 5.0
"""Seconds a Request waits for a proxy once none ever will be, while the Run drains."""


class NoProxyYet(NotYet):
    """No proxy is available yet; the Request is to wait elsewhere."""

    def __init__(self, delay: float) -> None:
        """Create the signal.

        Args:
            delay: Seconds until one may be.
        """
        super().__init__(delay, "no proxy available")


class RunProxies:
    """The proxies of a Run, from its sources, kept in its Proxy Pool and given to its fetches.

    When the Run starts, the proxies of every source are listed in the pool,
    without duplicates; a source that fails then fails the Run. Every `refresh`
    seconds, if set, the sources give them anew, and those they no longer give are
    no longer used; a refresh that fails is logged, and the proxies are kept. The
    proxies a source other than a list or a file gives that are not valid are
    skipped, with a warning. Their passwords are masked in the Run's log.

    Each fetch takes a proxy, or a Session is bound to one, and how the fetch went
    is reported: a `ProxyError` is a failure, and a Response tells that the proxy
    got through. When no proxy is available, the fetch waits until one stops cooling
    down. When none ever will be, with every proxy Retired and no refresh to bring
    others, the Run is stopped, draining it.
    """

    def __init__(
        self,
        pool: ProxyPool,
        sources: Sequence[tuple[str, ProxyProvider]],
        *,
        refresh: float | None,
        stats: Stats,
        stop: Callable[[], None],
        mask: Callable[[Iterable[str]], None],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the proxies to their pool and sources.

        Args:
            pool: The Proxy Pool.
            sources: The sources, each named as the settings name it, such as
                `proxies.file`.
            refresh: Seconds between loads of the sources after the first; `None` to
                load them once.
            stats: Where refreshes and their failures are counted, as
                `proxies.refreshes` and `proxies.refresh_failures`.
            stop: Stops the Run, draining it.
            mask: Masks values in the Run's log from now on.
            clock: The current time, in seconds since the epoch.
        """
        self._pool = pool
        self._sources = tuple(sources)
        self._refresh = refresh
        self._stats = stats
        self._stop = stop
        self._mask = mask
        self._clock = clock
        self._listed: frozenset[str] | None = None
        """The proxies listed by the last load; `None` before the first."""
        self._next_load = math.inf
        self._refreshing: asyncio.Task[None] | None = None
        self._exhausted = False

    async def open(self) -> None:
        """List the proxies of every source, then refresh them every `refresh` seconds, if set.

        Raises:
            Exception: A source failed, such as with a `pernix.errors.SettingsError`
                for a file with lines that are not proxies.
        """
        await self._load()
        if self._refresh is not None:
            self._refreshing = asyncio.create_task(self._refreshed(self._refresh), name="pernix-proxy-refresh")

    async def close(self) -> None:
        """Stop refreshing."""
        refreshing, self._refreshing = self._refreshing, None
        if refreshing is not None:
            refreshing.cancel()
            with suppress(asyncio.CancelledError):
                await refreshing

    async def take(self) -> str:
        """Take a proxy for one fetch.

        Returns:
            The proxy.

        Raises:
            NoProxyYet: None is available now.
        """
        proxy = await self._pool.take()
        if proxy is None:
            raise NoProxyYet(await self._unavailable())
        return proxy

    async def bind(self, session: str, current: str | None) -> str:
        """Return the proxy a Session goes through: its own while available, else another.

        Args:
            session: The Session's id.
            current: The proxy the Session is bound to, if any.

        Returns:
            The proxy.

        Raises:
            NoProxyYet: None is available now.
        """
        proxy = await self._pool.bind(session, current)
        if proxy is None:
            raise NoProxyYet(await self._unavailable())
        if current is not None and proxy != current:
            logger.info("The proxy of the Session %r is not available; it goes through another", session)
        return proxy

    @asynccontextmanager
    async def reporting(self, proxy: str, sending: Sending) -> AsyncGenerator[None]:
        """Report how the fetch inside the block went through a proxy, once it ends.

        A `ProxyError`, raised or recorded as the `error`, is a failure; a Response
        tells that the proxy got through. Anything else, such as a timeout, which a
        slow site causes as well, is not reported.

        Args:
            proxy: The proxy.
            sending: The fetch.

        Yields:
            Nothing; the fetch runs inside.
        """
        since = self._clock()
        error: BaseException | None = None
        try:
            yield
        except BaseException as exc:
            error = exc
            raise
        finally:
            error = error or sending.error
            if isinstance(error, ProxyError):
                await self._pool.report(proxy, failed=True, since=since)
            elif sending.response is not None:
                await self._pool.report(proxy, failed=False, since=since)

    async def _unavailable(self) -> float:
        """Seconds a fetch is to wait for a proxy; when none ever will be, stop the Run, once."""
        delay = await self._pool.ready_in()
        if delay < math.inf:
            return delay
        if self._refresh is not None:
            return max(self._next_load - self._clock(), 0.0)
        if not self._exhausted:
            self._exhausted = True
            logger.error(
                "No proxy can be used: %s, and none are loaded again; stopping the Run",
                "every proxy is retired" if self._listed else "the sources gave none",
            )
            self._stop()
        return _EXHAUSTED_WAIT

    async def _refreshed(self, interval: float) -> None:
        """Load the sources every `interval` seconds, keeping the proxies when that fails."""
        while True:
            await asyncio.sleep(max(self._next_load - self._clock(), 0.0))
            try:
                await self._load()
            except Exception as exc:
                self._next_load = self._clock() + interval
                self._stats.inc("proxies.refresh_failures")
                logger.error("Refreshing the proxies failed; the ones loaded before are kept", exc_info=exc)
            else:
                self._stats.inc("proxies.refreshes")
                if await self._pool.ready_in() == math.inf:
                    logger.warning("No proxy can be used; Requests wait for the next refresh, in %.0f s", interval)

    async def _load(self) -> None:
        """List the proxies of every source in the pool, masking their passwords first."""
        proxies: dict[str, None] = {}
        for name, source in self._sources:
            proxies.update(dict.fromkeys(_valid(name, await source.proxies())))
        listed = frozenset(proxies)
        before = self._listed or frozenset()
        self._mask(secret for proxy in listed - before for secret in proxy_credentials(proxy))
        await self._pool.replace(list(proxies))
        if self._listed is None:
            logger.info("Loaded %d proxies", len(listed))
        else:
            logger.info(
                "Refreshed the proxies: %d, %d of them new, and %d no longer listed",
                len(listed),
                len(listed - before),
                len(before - listed),
            )
        self._listed = listed
        if self._refresh is not None:
            self._next_load = self._clock() + self._refresh


def _valid(source: str, proxies: Iterable[object]) -> list[str]:
    """The proxies a source gave, as the pool holds them, skipping with a warning those that are not valid."""
    valid: list[str] = []
    problems: list[str] = []
    for position, proxy in enumerate(proxies, start=1):
        if not isinstance(proxy, str):
            problems.append(f"#{position} (not a string)")
            continue
        try:
            valid.append(proxy_url(proxy))
        except ValueError as exc:
            problems.append(f"#{position} ({exc})")
    if problems:
        logger.warning("Skipped the proxies %s gave that are not valid: %s", source, ", ".join(problems))
    return valid
