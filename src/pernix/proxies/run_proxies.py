"""The proxies of a Run: loaded from its sources into its Proxy Pool, refreshed, and given to its fetches."""

from __future__ import annotations

import asyncio
import logging
import math
import time
from contextlib import asynccontextmanager, suppress
from typing import TYPE_CHECKING

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

__all__ = ["FollowedProxy", "NoProxyYet", "RunProxies"]

logger = logging.getLogger(__name__)

_EXHAUSTED_WAIT = 5.0
"""Seconds a Request waits for a proxy once none ever will be, while the Run drains."""
_UNFIT_WAIT = 1.0
"""Seconds a Request waits when no proxy fits its Session, though some are available."""
_BIND_ATTEMPTS = 3
"""Times the binding policy is asked for one binding, while the proxies it picks are taken first."""
_GONE: dict[BindingReason, str] = {"cooldown": "is cooling down", "retired": "is retired", "unlisted": "is not listed"}


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
    """The binding policy keeps a Session on its proxy, which cools down, is Retired, or is no longer listed.

    The Session follows its proxy: it cools down with it until `until`, or, once
    the proxy is Retired or no longer listed, it is Retired with it.

    Attributes:
        until: When the proxy stops cooling down, in seconds since the epoch;
            `None` once it is Retired, or no longer listed.
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
            stats: Where refreshes, their failures, and the bindings that wait are
                counted, as `proxies.refreshes`, `proxies.refresh_failures`, and
                `proxies.binding_waits`.
            stop: Stops the Run, draining it.
            mask: Masks values in the Run's log from now on.
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
        self._listed: frozenset[str] | None = None
        """The proxies listed by the last load; `None` before the first."""
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
                return self._bound(binding, picked)
        if self._sources and not any(candidate.health == "available" and not candidate.own for candidate in candidates):
            raise NoProxyYet(await self._unavailable(binding=True))
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
        if held is None and isinstance(picked, Proxy):
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
            if any(each.url == proxy and each.own and each.health != "retired" for each in candidates):
                return True
        return False

    async def _unavailable(self, *, binding: bool = False) -> float:
        """Seconds a fetch is to wait for a proxy; when none ever will be, stop the Run, once.

        A Session being bound waits as when no proxy fits it, rather than stop the
        Run, while other Sessions go through proxies of their own.
        """
        delay = await self._pool.ready_in()
        if delay < math.inf:
            return delay
        if self._refresh is not None:
            return max(self._next_load - self._clock(), 0.0)
        if binding and await self._owned():
            return _UNFIT_WAIT
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
        proxies: dict[str, Proxy] = {}
        for name, source in self._sources:
            for proxy in _valid(name, await source.proxies()):
                proxies.setdefault(proxy.url, proxy)
        listed = frozenset(proxies)
        before = self._listed or frozenset()
        self._mask(secret for proxy in listed - before for secret in proxy_credentials(proxy))
        await self._pool.replace(list(proxies.values()))
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
