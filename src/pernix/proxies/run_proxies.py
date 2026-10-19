"""The proxies of a Run: loaded from its sources into its Proxy Pool, refreshed, and given to its fetches."""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import msgspec

from pernix.errors import ProxyError
from pernix.proxies.binding_policy import Binding
from pernix.proxies.proxy import Proxy
from pernix.proxies.proxy_url import proxy_credentials, proxy_url
from pernix.sending import NotYet

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterable, Sequence

    from pernix.account import Account
    from pernix.proxies.binding_policy import BindingPolicy, BindingReason
    from pernix.proxies.proxy_pool import ProxyCandidate, ProxyPool
    from pernix.proxies.proxy_sources import ProxyProvider
    from pernix.sending import Sending
    from pernix.stats import Stats

__all__ = ["FollowedProxy", "NoProxyYet", "ProxySupply", "RunProxies"]

logger = logging.getLogger(__name__)

_EXHAUSTED_WAIT = 5.0
"""Seconds a Request waits for a proxy once none ever will be, while the Run drains."""
_UNFIT_WAIT = 1.0
"""Seconds a Request waits when no proxy fits its Session, though some are available."""
_EXTRACTION_WAIT = 0.5
"""Seconds a Request waits for an extraction under way, beyond the time until it may start."""
_BIND_ATTEMPTS = 3
"""Times the binding policy is asked for one binding, while the proxies it picks are taken first."""
_GONE: dict[BindingReason, str] = {
    "cooldown": "is cooling down",
    "retired": "is retired",
    "expired": "has expired",
    "unlisted": "is not listed",
}


@dataclass(frozen=True, slots=True)
class ProxySupply:
    """How the proxies of a Run are supplied: whether loads add or replace them, and extracting more on demand.

    Attributes:
        add: Whether each load adds the proxies the sources give to those listed,
            which stay until they expire, rather than replace them.
        extracting: The names of the sources that extract proxies, such as
            `proxies.api`: they are told how many are wanted, loaded on demand, and
            what they give counts against `max_extracted`.
        min_available: Extract on demand while fewer proxies are available, and when
            a fetch finds none; `None` to extract only at the loads.
        interval: Seconds at least between the starts of two extractions on demand,
            or after a load.
        max_extracted: Proxies the extracting sources give at most in the Run;
            `None` for no limit.
        attempts: Extractions in a row that bring no proxy, after which no more are
            made on demand.
    """

    add: bool = False
    extracting: frozenset[str] = frozenset()
    min_available: int | None = None
    interval: float = 1.0
    max_extracted: int | None = None
    attempts: int = 3


class NoProxyYet(NotYet):
    """No proxy is available yet, or none fits the Session; the Request is to wait elsewhere."""

    def __init__(self, delay: float, reason: str = "no proxy available") -> None:
        """Create the signal.

        Args:
            delay: Seconds until one may be.
            reason: Why none is given.
        """
        super().__init__(delay, reason)


class FollowedProxy(NotYet):
    """The binding policy keeps a Session on its proxy, which cools down, is Retired, expired, or is no longer listed.

    The Session follows its proxy: it cools down with it until `until`, or, once
    the proxy is Retired, expired, or no longer listed, it is Retired with it.

    Attributes:
        until: When the proxy stops cooling down, in seconds since the epoch;
            `None` once it is Retired, expired, or no longer listed.
    """

    def __init__(self, until: float | None, *, now: float) -> None:
        """Create the signal.

        Args:
            until: When the proxy stops cooling down; `None` once it is gone.
            now: The current time, in seconds since the epoch.
        """
        if until is None:
            super().__init__(0.0, "the Session is retired with its proxy")
        else:
            super().__init__(max(until - now, 0.0), "the Session waits for its proxy")
        self.until = until


class RunProxies:
    """The proxies of a Run, from its sources, kept in its Proxy Pool and given to its fetches.

    When the Run starts, the proxies of every source are listed in the pool,
    without duplicates; a source that fails then fails the Run. Every `refresh`
    seconds, if set, the sources give them anew, and those they no longer give are
    no longer used; a refresh that fails is logged, and the proxies are kept. The
    proxies a source other than a list or a file gives that are not valid are
    skipped, with a warning. Their passwords are masked in the Run's log.

    Each fetch takes a proxy, or a Session is bound to one, as the binding policy
    picks it, and how the fetch went is reported: a `ProxyError` is a failure, and
    a Response tells that the proxy got through. A Session the policy keeps on a
    proxy that cools down, is Retired, or is no longer listed follows it. When no
    proxy is available, the fetch waits until one stops cooling down. When none
    ever will be, with every proxy Retired and no refresh to bring others, the Run
    is stopped, draining it, unless Sessions go on through proxies of their own.
    When the policy finds no proxy for a Session, though some are available, or no
    source is set, its fetch waits a second, which is counted as
    `proxies.binding_waits` and logged once until the Session is bound again.

    As `supply` says, each load adds the proxies the sources give rather than
    replace them, and the sources that extract proxies are told how many are
    wanted. While fewer than `supply.min_available` proxies are available, or when
    a fetch finds none, they are extracted on demand, one extraction at a time and
    `supply.interval` seconds at least apart, until they have given
    `supply.max_extracted` proxies, or `supply.attempts` extractions in a row gave
    none; the Run is stopped for want of proxies only then. Extractions are counted
    as `proxies.extractions`, and the proxies they gave as `proxies.extracted`.
    """

    def __init__(
        self,
        pool: ProxyPool,
        sources: Sequence[tuple[str, ProxyProvider]],
        *,
        policy: BindingPolicy,
        refresh: float | None,
        stats: Stats,
        stop: Callable[[], None],
        mask: Callable[[Iterable[str]], None],
        supply: ProxySupply | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Wire the proxies to their pool and sources.

        Args:
            pool: The Proxy Pool.
            sources: The sources, each named as the settings name it, such as
                `proxies.file`.
            policy: Picks the proxy each Session is bound to.
            refresh: Seconds between loads of the sources after the first; `None` to
                load them once.
            stats: Where refreshes, extractions, their failures, and the bindings
                that wait are counted, as `proxies.refreshes`,
                `proxies.refresh_failures`, `proxies.extractions`,
                `proxies.extracted`, `proxies.extraction_failures`, and
                `proxies.binding_waits`.
            stop: Stops the Run, draining it.
            mask: Masks values in the Run's log from now on.
            supply: Whether loads add the proxies or replace them, and how more are
                extracted; replaced, and none extracted on demand, when `None`.
            clock: The current time, in seconds since the epoch.
        """
        self._pool = pool
        self._sources = tuple(sources)
        self._policy = policy
        self._refresh = refresh
        self._stats = stats
        self._stop = stop
        self._mask = mask
        self._clock = clock
        self._supply = supply or ProxySupply()
        self._listed: frozenset[str] | None = None
        """The proxies listed by the last load; `None` before the first."""
        self._seen: set[str] = set()
        """The proxies the sources gave, whose passwords are masked."""
        self._extracted = 0
        """The proxies the extracting sources gave."""
        self._fruitless = 0
        """Extractions in a row that gave no proxy."""
        self._last_extraction = -math.inf
        """When the last extraction started."""
        self._extracting: asyncio.Task[None] | None = None
        self._loading = asyncio.Lock()
        """Held by each load, so that a refresh and an extraction never ask the sources at once."""
        self._next_load = math.inf
        self._refreshing: asyncio.Task[None] | None = None
        self._exhausted = False
        self._waiting: set[str] = set()
        """The Sessions told to wait for a proxy that fits, since they were last bound."""
        self._own: dict[str, str] = {}
        """The proxy of its own each Session was last bound to, by its id."""

    async def open(self) -> None:
        """List the proxies of every source, then refresh them every `refresh` seconds, if set.

        Raises:
            Exception: A source failed, such as with a `pernix.errors.SettingsError`
                for a file with lines that are not proxies.
        """
        if not self._sources:
            logger.info("No source of proxies is set; Sessions go through those the binding policy gives them")
            return
        await self._load()
        if self._refresh is not None:
            self._refreshing = asyncio.create_task(self._refreshed(self._refresh), name="pernix-proxy-refresh")

    async def close(self) -> None:
        """Stop refreshing, and extracting."""
        for task in (self._refreshing, self._extracting):
            if task is not None:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        self._refreshing = self._extracting = None

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
        if self._supply.min_available is not None:
            self._keep_level(await self.available())
        return proxy

    async def bind(self, session: str, current: str | None, *, account: Account | None = None) -> str:
        """Bind a Session to the proxy the binding policy picks for one of its Leases, and return it.

        The Lease holds the proxy until `release()`.

        Args:
            session: The Session's id.
            current: The proxy the Session's state names, if any; the pool's
                binding goes first.
            account: The Account the Session logs in with, if any.

        Returns:
            The proxy.

        Raises:
            FollowedProxy: The policy keeps the Session on its proxy, which cools
                down, is Retired, or is no longer listed.
            NoProxyYet: The policy picked none, as when none is available now, or
                those it picked were taken first by other Sessions.
            ValueError: The policy picked a URL the pool does not list, or gave the
                Session a proxy of its own that is not valid, or that the pool
                lists.
            TypeError: The policy returned neither a URL, a `Proxy`, nor `None`.
            Exception: Whatever the policy raised.
        """
        candidates: tuple[ProxyCandidate, ...] = ()
        for _ in range(_BIND_ATTEMPTS):
            candidates = tuple(await self._pool.candidates(session))
            binding = _binding(session, account, current, candidates, self._pool.max_sessions)
            picked = self._checked(binding, await self._policy.bind(binding))
            if picked is None:
                break
            followed = self._followed(binding, picked)
            if followed is not None:
                raise followed
            if await self._pool.bind(session, picked):
                self._keep_level(sum(each.health == "available" and not each.own for each in candidates))
                return self._bound(binding, picked)
        if self._sources and not any(candidate.health == "available" and not candidate.own for candidate in candidates):
            raise NoProxyYet(await self._unavailable(binding=True))
        self._extract()  # none fits it
        self._stats.inc("proxies.binding_waits")
        if session not in self._waiting:
            self._waiting.add(session)
            logger.warning("No proxy fits the Session %r now; its Requests wait for one", session)
        raise NoProxyYet(_UNFIT_WAIT, "no proxy fits the Session")

    async def following(self, session: str, proxy: str, *, account: Account | None = None) -> FollowedProxy | None:
        """Tell whether a Session follows its proxy, once a fetch through it failed.

        The binding policy is asked, as it is at the next Lease, while the proxy
        cools down or is Retired.

        Args:
            session: The Session's id.
            proxy: The proxy its Lease was bound to.
            account: The Account the Session logs in with, if any.

        Returns:
            What the Session is to do, as `bind()` would tell it; `None` while the
            proxy is available, once the Session is bound to another, or when the
            policy binds it to another at its next Lease.

        Raises:
            ValueError: The policy picked a URL the pool does not list, or gave the
                Session a proxy of its own that is not valid, or that the pool
                lists.
            TypeError: The policy returned neither a URL, a `Proxy`, nor `None`.
            Exception: Whatever the policy raised.
        """
        candidates = tuple(await self._pool.candidates(session))
        binding = _binding(session, account, proxy, candidates, self._pool.max_sessions)
        if binding.current != proxy or binding.reason not in _GONE:
            return None  # bound to another since, or its proxy is available
        picked = self._checked(binding, await self._policy.bind(binding))
        return None if picked is None else self._followed(binding, picked)

    async def release(self, session: str, proxy: str) -> None:
        """End the hold on a proxy of a Lease that `bind()` bound a Session to it for, once the Lease ends.

        Args:
            session: The Session's id.
            proxy: The proxy the Lease was bound to.
        """
        await self._pool.release(session, proxy)

    async def unbind(self, session: str) -> None:
        """Release the binding of a Session, such as one Retired, so it no longer counts toward its proxy's Sessions.

        Args:
            session: The Session's id.
        """
        self._waiting.discard(session)
        self._own.pop(session, None)
        await self._pool.unbind(session)

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

    def _checked(self, binding: Binding, picked: object) -> str | Proxy | None:
        """What the binding policy picked, checked: a URL among the candidates, or a valid proxy of the Session's own.

        The password of a proxy of its own is masked in the Run's log from now on.
        """
        if picked is None:
            return None
        if isinstance(picked, str):
            if picked != binding.current and not any(candidate.url == picked for candidate in binding.candidates):
                raise ValueError("the binding policy picked a proxy the Proxy Pool does not list")
            return picked
        if not isinstance(picked, Proxy):
            raise TypeError(
                f"the binding policy must return a proxy URL, a Proxy, or None, not {type(picked).__name__}"
            )
        try:
            own = _proxy(picked)
        except ValueError as exc:
            raise ValueError(
                f"the binding policy gave the Session {binding.session!r} a proxy of its own that is not valid: {exc}"
            ) from None
        if any(candidate.url == own.url and not candidate.own for candidate in binding.candidates):
            raise ValueError(
                f"the binding policy gave the Session {binding.session!r} as its own a proxy the Proxy Pool lists"
            )
        self._mask(proxy_credentials(own.url))
        return own

    def _followed(self, binding: Binding, picked: str | Proxy) -> FollowedProxy | None:
        """Whether a Session follows its proxy: the policy kept it on one that cools down, is Retired, or is gone.

        A record of a proxy the pool does not hold for the Session has it held anew instead.
        """
        url = picked if isinstance(picked, str) else picked.url
        if url != binding.current or binding.reason not in _GONE:
            return None
        held = next((candidate for candidate in binding.candidates if candidate.url == url), None)
        if isinstance(picked, Proxy) and (held is None or (binding.reason == "expired" and self._lasts(picked))):
            return None
        if binding.reason == "cooldown":
            until = self._clock() + _UNFIT_WAIT if held is None or held.until is None else held.until
            logger.info("The proxy of the Session %r is cooling down; the Session waits for it", binding.session)
            return FollowedProxy(until, now=self._clock())
        logger.warning(
            "The proxy of the Session %r %s; the Session is retired with it", binding.session, _GONE[binding.reason]
        )
        return FollowedProxy(None, now=self._clock())

    def _bound(self, binding: Binding, picked: str | Proxy) -> str:
        """Note that a Session is bound to the proxy picked, and return its URL."""
        session = binding.session
        proxy = picked if isinstance(picked, str) else picked.url
        self._waiting.discard(session)
        own = isinstance(picked, Proxy) or any(
            candidate.url == proxy and candidate.own for candidate in binding.candidates
        )
        if own:
            self._own[session] = proxy
        else:
            self._own.pop(session, None)
        if binding.reason in _GONE and proxy != binding.current:
            logger.info("The proxy of the Session %r %s; it goes through another", session, _GONE[binding.reason])
        return proxy

    async def _owned(self) -> bool:
        """Whether a Session goes through a proxy of its own that is not Retired."""
        for session, proxy in self._own.items():
            candidates = await self._pool.candidates(session)
            if any(each.url == proxy and each.own and each.health in ("available", "cooldown") for each in candidates):
                return True
        return False

    def _lasts(self, proxy: Proxy) -> bool:
        """Whether a proxy has not expired, as a record gives it."""
        return proxy.expires is None or proxy.expires > self._clock()

    async def _unavailable(self, *, binding: bool = False) -> float:
        """Seconds a fetch is to wait for a proxy; when none ever will be, stop the Run, once.

        While proxies can be extracted on demand, one is, and the fetch waits for it.
        A Session being bound waits as when no proxy fits it, rather than stop the
        Run, while other Sessions go through proxies of their own.
        """
        delay = await self._pool.ready_in()
        if self._extract():
            wait = max(self._last_extraction + self._supply.interval - self._clock(), 0.0) + _EXTRACTION_WAIT
            return min(delay, wait)
        if delay < math.inf:
            return delay
        if self._refresh is not None and self._refreshes_bring_proxies():
            return max(self._next_load - self._clock(), 0.0)
        if binding and await self._owned():
            return _UNFIT_WAIT
        if not self._exhausted:
            self._exhausted = True
            logger.error(
                "No proxy can be used: %s, and %s; stopping the Run",
                "every proxy is retired or expired" if self._seen else "the sources gave none",
                "no more can be extracted" if self._supply.min_available is not None else "none are loaded again",
            )
            self._stop()
        return _EXHAUSTED_WAIT

    def _keep_level(self, available: int) -> None:
        """Extract on demand while fewer proxies than the level are available."""
        level = self._supply.min_available
        if level is not None and available < level:
            self._extract(needed=False)

    def _extract(self, *, needed: bool = True) -> bool:
        """Extract proxies on demand, unless an extraction is under way; whether one is, or may still bring proxies.

        Args:
            needed: Whether a fetch found no proxy, so that at least one is wanted,
                however many are available.
        """
        if not self._may_extract(on_demand=True):
            return False
        if self._extracting is None or self._extracting.done():
            self._extracting = asyncio.create_task(self._extraction(needed=needed), name="pernix-proxy-extraction")
        return True

    def _may_extract(self, *, on_demand: bool) -> bool:
        """Whether the extracting sources may be loaded: within the limit, and on demand, until they give none."""
        supply = self._supply
        if not supply.extracting or (supply.max_extracted is not None and self._extracted >= supply.max_extracted):
            return False
        if on_demand:
            return supply.min_available is not None and self._fruitless < supply.attempts
        return True

    def _refreshes_bring_proxies(self) -> bool:
        """Whether a refresh may bring proxies: from a source that does not extract, or one that may extract still."""
        extracting = self._supply.extracting
        return any(name not in extracting for name, _ in self._sources) or self._may_extract(on_demand=False)

    async def _extraction(self, *, needed: bool) -> None:
        """Extract proxies, once the interval since the last extraction passed, unless enough are available by then."""
        interval = self._supply.interval
        while True:
            await asyncio.sleep(max(self._last_extraction + interval - self._clock(), 0.0))
            async with self._loading:
                if self._clock() < self._last_extraction + interval:
                    continue  # a refresh extracted meanwhile
                level = self._supply.min_available or 0
                available = await self.available()
                if not needed and available >= level:
                    return
                try:
                    await self._load_now(wanted=max(level - available, 1))
                except Exception as exc:
                    self._stats.inc("proxies.extraction_failures")
                    logger.error("Extracting proxies failed", exc_info=exc)
                return

    async def available(self) -> int:
        """Return how many of the proxies listed, which no Session holds as its own, are available.

        Returns:
            The count.
        """
        return sum(candidate.health == "available" for candidate in await self._pool.candidates())

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
        """List the proxies every source gives in the pool, one load at a time."""
        async with self._loading:
            await self._load_now()

    async def _load_now(self, *, wanted: int | None = None) -> None:
        """List the proxies the sources give in the pool, adding or replacing them, masking their passwords first.

        Args:
            wanted: How many proxies to extract, on demand, from the extracting
                sources alone; `None` to load every source, the extracting ones
                within the limit, told the level.
        """
        on_demand = wanted is not None
        proxies = await self._given(on_demand=on_demand, wanted=wanted or self._supply.min_available)
        given = frozenset(proxies)
        new = given - self._seen
        self._mask(secret for proxy in new for secret in proxy_credentials(proxy))
        self._seen |= new
        if self._supply.add:
            await self._pool.add(list(proxies.values()))
        else:
            await self._pool.replace(list(proxies.values()))
        if on_demand:
            logger.info("Extracted %d proxies, %d of them new", len(given), len(new))
        elif self._listed is None:
            logger.info("Loaded %d proxies", len(given))
        elif self._supply.add:
            logger.info("Refreshed the proxies: %d added, %d of them new", len(given), len(new))
        else:
            before = self._listed
            logger.info(
                "Refreshed the proxies: %d, %d of them new, and %d no longer listed",
                len(given),
                len(given - before),
                len(before - given),
            )
        if not on_demand:
            self._listed = given
            if self._refresh is not None:
                self._next_load = self._clock() + self._refresh

    async def _given(self, *, on_demand: bool, wanted: int | None) -> dict[str, Proxy]:
        """The proxies the sources give, without duplicates, the first given of each kept.

        The extracting sources are asked for `wanted` proxies in all, one after
        another, within the limit, each counted as it answers; on demand, only they
        are.
        """
        supply = self._supply
        proxies: dict[str, Proxy] = {}
        for name, source in self._sources:
            if name in supply.extracting:
                asked = self._within_limit(wanted)
                if not self._may_extract(on_demand=on_demand) or (asked is not None and asked <= 0):
                    continue
                self._last_extraction = self._clock()
                try:
                    given = _valid(name, await _extract(source, asked))
                except Exception:
                    self._extracted_now(0)
                    raise
                self._extracted_now(len(given))
                if wanted is not None:
                    wanted -= len(given)
            elif on_demand:
                continue
            else:
                given = _valid(name, await source.proxies())
            for proxy in given:
                proxies.setdefault(proxy.url, proxy)
        return proxies

    def _within_limit(self, wanted: int | None) -> int | None:
        """How many proxies to ask the next extracting source for: those wanted, as many as the limit leaves at most."""
        limit = self._supply.max_extracted
        if limit is None or wanted is None:
            return wanted
        return min(wanted, limit - self._extracted)

    def _extracted_now(self, count: int) -> None:
        """Count an extraction and the proxies it gave, warning once no more can be extracted on demand."""
        supply = self._supply
        self._stats.inc("proxies.extractions")
        self._stats.inc("proxies.extracted", count)
        self._extracted += count
        self._fruitless = 0 if count else self._fruitless + 1
        if self._fruitless == supply.attempts and supply.min_available is not None:
            logger.warning("%d extractions in a row gave no proxy; no more are made on demand", self._fruitless)
        limit = supply.max_extracted
        if count and limit is not None and self._extracted >= limit > self._extracted - count:
            logger.warning(
                "The sources extracted %d proxies, reaching the limit; no more are extracted", self._extracted
            )


def _binding(
    session: str,
    account: Account | None,
    current: str | None,
    candidates: tuple[ProxyCandidate, ...],
    max_sessions: int | None,
) -> Binding:
    """What the binding policy is told of a Session: the pool's binding goes before the one its state names."""
    held = next((candidate for candidate in candidates if session in candidate.sessions), None)
    if held is None:
        held = next((candidate for candidate in candidates if candidate.url == current), None)
    else:
        current = held.url
    return Binding(
        session,
        account=account,
        current=current,
        health=None if held is None else held.health,
        reason=_reason(current, held),
        candidates=candidates,
        max_sessions=max_sessions,
    )


def _reason(current: str | None, held: ProxyCandidate | None) -> BindingReason:
    """Why a Session is being bound, given the proxy it is bound to and that proxy as the pool holds it."""
    if current is None:
        return "new"
    if held is None:
        return "unlisted"
    return "lease" if held.health == "available" else held.health


async def _extract(source: ProxyProvider, wanted: int | None) -> Iterable[object]:
    """What a source that extracts proxies gives, told how many are wanted if it takes `wanted`."""
    if wanted is not None and _takes_wanted(source):
        return await cast("Any", source).proxies(wanted=wanted)
    return await source.proxies()


def _takes_wanted(source: ProxyProvider) -> bool:
    """Whether a source's `proxies()` takes a `wanted` keyword."""
    try:
        parameters = inspect.signature(source.proxies).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        or (parameter.name == "wanted" and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY)
        for parameter in parameters
    )


def _valid(source: str, proxies: Iterable[object]) -> list[Proxy]:
    """The proxies a source gave, as the pool holds them, skipping with a warning those that are not valid."""
    valid: list[Proxy] = []
    problems: list[str] = []
    for position, proxy in enumerate(proxies, start=1):
        try:
            valid.append(_proxy(proxy))
        except ValueError as exc:
            problems.append(f"#{position} ({exc})")
    if problems:
        logger.warning("Skipped the proxies %s gave that are not valid: %s", source, ", ".join(problems))
    return valid


def _proxy(given: object) -> Proxy:
    """A proxy a source gave, as a URL or a record, as the pool holds it.

    Raises:
        ValueError: It is neither a proxy URL nor a valid `Proxy` record of one; the
            message does not quote it.
    """
    if isinstance(given, str):
        return Proxy(proxy_url(given))
    if not isinstance(given, Proxy):
        raise ValueError("not a string or a Proxy")
    # The fields of a record built in code are not checked, as those of one decoded are.
    record = msgspec.convert(msgspec.structs.asdict(given), Proxy)
    return msgspec.structs.replace(record, url=proxy_url(record.url))
