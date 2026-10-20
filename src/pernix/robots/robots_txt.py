"""Obeying the robots.txt of each origin a Run's Requests go to, as RFC 9309 has it (`robots.obey`)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pernix.errors import FetchError, IgnoreRequest, NetworkBlocked, ProxyError, TooManyRedirects
from pernix.fetchers.fetcher_dispatch import Streams, streaming_into
from pernix.fetchers.redirects import following_at_most
from pernix.frontiers import host_slot
from pernix.middlewares import Middleware
from pernix.request import Request
from pernix.retry_policy import retry_after_seconds
from pernix.robots.robots_rules import PARSE_LIMIT, Access, RobotsRules, access, is_robots_txt
from pernix.sending import NotYet
from pernix.urls import url_origin

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.fetchers import StreamedResponse
    from pernix.middlewares import Download
    from pernix.response import Response
    from pernix.settings import RobotsSettings
    from pernix.stats import Stats
    from pernix.throttle import Throttle

__all__ = ["CACHE_TTL", "MAX_REDIRECTS", "UNREACHABLE_LIMIT", "UNREACHABLE_WAIT", "RobotsTxt"]

logger = logging.getLogger(__name__)

CACHE_TTL = 24 * 3600.0
"""Seconds the rules of a robots.txt are kept before it is fetched again (RFC 9309 §2.4)."""

MAX_REDIRECTS = 5
"""Redirects a robots.txt fetch follows; beyond them, it is taken to be unavailable (RFC 9309 §2.3.1.2)."""

UNREACHABLE_WAIT = 60.0
"""Seconds the Requests to an origin wait for its unreachable robots.txt, unless a `Retry-After` names others."""

UNREACHABLE_LIMIT = 3
"""Unreachable answers in a row after which an origin whose rules were never fetched is disallowed for `CACHE_TTL`."""

REASON = "robots.txt"
"""The reason of the `IgnoreRequest` raised for a Request robots.txt disallows."""

_SCHEMES = frozenset({"http", "https"})


@dataclass(slots=True)
class _Origin:
    """What is known of one origin's robots.txt."""

    rules: RobotsRules | None = None
    """The rules in force, those fetched last or those unreachable answers led to; `None` while none are."""
    refresh_at: float = 0.0
    """When robots.txt is to be fetched again; until then, a Request without rules in force waits."""
    unreachable: int = 0
    """Unreachable answers in a row while no rules were in force."""
    fetching: asyncio.Event | None = None
    """Set once the fetch of robots.txt in progress, if any, ends."""


class RobotsTxt(Middleware):
    """Obeys the robots.txt of each origin, from outermost in a Run's download chain, with `robots.obey`.

    The Run places it there, so it sees every fetch, from the Frontier, an Inline
    Fetch, or rendered, as it was claimed, after the fetch took its place in its
    slot, its Session, and its proxy, and was paced. Only http and https URLs are
    checked; a Request for an origin's `/robots.txt` itself, and one with
    `ext={"robots": {"obey": False}}`, go through as they are.

    The first fetch to an origin whose rules are not known, or are older than
    `CACHE_TTL`, fetches its robots.txt, as part of itself, so as the same client:
    through the rest of the download chain, with the `http` Fetcher, under its
    Session, if any, and through its proxy, sharing its place in the slot and the
    start it was paced for, and following five redirects at most. Its Response is
    judged neither Blocked nor logged out, but the cookies it sets are kept in the
    Session. The other fetches to the origin meanwhile wait for it; if it fails as
    the Request's own fetch would, they fetch it in turn. Each fetch that fetched
    robots.txt, or waited for it, is paced again before it starts, as robots.txt
    took a start of its domain.

    What the answer means (RFC 9309 §2.3.1), from the first `PARSE_LIMIT` bytes
    of the body:

    - A 2xx: its rules, for the product token of `robots.user_agent`.
    - Another 4xx, a status that does not redirect, or more than five redirects:
      there is no robots.txt for the crawler, and everything is allowed.
    - A 5xx, a 429, or a fetch that failed (a `FetchError`): it is unreachable,
      and fetched again after `UNREACHABLE_WAIT` seconds, or as long as a
      `Retry-After` says, at most `domain.cooldown_max`. Rules fetched before stay
      in force meanwhile (RFC 9309 §2.4). Without them, the domain cools down that
      long, and its Requests wait, raising `NotYet`: those from the Frontier there,
      without counting an attempt, and Inline Fetches in place; after
      `UNREACHABLE_LIMIT` unreachable answers in a row, the origin is disallowed
      for `CACHE_TTL`.
    - A `ProxyError` or a `NetworkBlocked`, which tell of the Request's way rather
      than of the site, and an exception other than a `FetchError`, are raised as
      the Request's own, which is retried as its fetch failing would be.

    A Request the rules disallow raises `IgnoreRequest` with the reason
    `"robots.txt"`. The `Crawl-delay` of the rules spaces the starts of the
    domain's fetches when it is longer than the jittered `domain.delay`, at most
    `robots.crawl_delay_max`; the domain takes the longest of its origins'.

    Stats: `robots.fetched`, the fetches of robots.txt; `robots.by_status.<code>`,
    their statuses; `robots.errors_by_type.<ErrorType>`, those that raised;
    `robots.disallowed`, the fetches disallowed; and `robots.deferred`, the
    fetches sent to wait for an unreachable robots.txt.
    """

    def __init__(
        self,
        settings: RobotsSettings,
        *,
        throttle: Throttle,
        stats: Stats,
        cooldown_max: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Obey robots.txt for a Run.

        Args:
            settings: The `robots.*` settings.
            throttle: Paces the fetches of each domain, which a `Crawl-delay`
                spaces, and an unreachable robots.txt cools down.
            stats: Where robots.txt fetches and their outcomes are counted.
            cooldown_max: The longest the Requests to an origin wait for its
                unreachable robots.txt, as `domain.cooldown_max` limits cooldowns.
            clock: A monotonic clock, in seconds.
        """
        self._user_agent = settings.user_agent
        self._crawl_delay_max = settings.crawl_delay_max
        self._throttle = throttle
        self._stats = stats
        self._cooldown_max = cooldown_max
        self._clock = clock
        self._origins: dict[str, _Origin] = {}
        self._delays: dict[str, dict[str, float]] = {}  # the Crawl-delay of each origin, by slot
        for key in ("robots.fetched", "robots.disallowed", "robots.deferred"):
            stats.set(key, 0)

    async def __call__(self, request: Request, call_next: Download) -> Response:
        """Fetch a Request if the robots.txt of its origin allows it.

        Args:
            request: The Request, as it is sent.
            call_next: Fetches a Request through the rest of the download chain.

        Returns:
            The Response.

        Raises:
            pernix.errors.IgnoreRequest: robots.txt disallows the Request.
            pernix.sending.NotYet: The Request is to wait, as its robots.txt is
                unreachable, or its domain started cooling down while it waited.
            Exception: Fetching robots.txt raised what the Request's own fetch
                raises too, such as a `ProxyError`.
        """
        origin = _origin(request)
        if origin is None or is_robots_txt(request.url) or not _obeys(request):
            return await call_next(request)
        rules, waited = await self._rules(origin, request, call_next)
        if not rules.allows(request.url):
            self._stats.inc("robots.disallowed")
            raise IgnoreRequest(REASON)
        slot = host_slot(request)
        if waited and not await self._throttle.pace(slot):
            raise NotYet(self._throttle.cooldown_left(slot), "domain cooling down")
        return await call_next(request)

    async def _rules(self, origin: str, request: Request, call_next: Download) -> tuple[RobotsRules, bool]:
        """The rules in force for an origin, fetching its robots.txt if they are due; and whether it waited for them.

        Raises:
            pernix.sending.NotYet: robots.txt is unreachable, and no rules are in force.
        """
        state = self._origins.setdefault(origin, _Origin())
        waited = False
        while True:
            now = self._clock()
            if now < state.refresh_at:
                if state.rules is not None:
                    return state.rules, waited
                raise self._deferred(state.refresh_at - now)
            if state.fetching is None:
                break
            waited = True
            await state.fetching.wait()
        state.fetching = fetching = asyncio.Event()
        try:
            rules = await self._fetch(origin, state, request, call_next)
        finally:
            state.fetching = None
            fetching.set()
        if rules is None:
            raise self._deferred(max(0.0, state.refresh_at - self._clock()))
        return rules, True

    async def _fetch(self, origin: str, state: _Origin, request: Request, call_next: Download) -> RobotsRules | None:
        """Fetch the robots.txt of an origin as part of the fetch of a Request, and keep what it tells.

        Returns:
            The rules in force; `None` while robots.txt is unreachable and no rules are.

        Raises:
            Exception: Fetching raised a `ProxyError`, a `NetworkBlocked`, or an
                exception other than a `FetchError`.
        """
        robots = Request(
            f"{origin}/robots.txt",
            fetcher="http",
            headers={"Referer": ""},  # typed in, from no page, whatever Callback runs
            session=request.session,
            proxy=request.proxy,
            impersonate=request.impersonate,
        )
        self._stats.inc("robots.fetched")
        try:
            response, body, cut = await self._download(robots, call_next)
        except Exception as exc:
            self._stats.inc(f"robots.errors_by_type.{type(exc).__name__}")
            if isinstance(exc, (ProxyError, NetworkBlocked)) or not isinstance(exc, FetchError):
                raise
            if not isinstance(exc, TooManyRedirects):
                return self._unreachable(origin, state, request, f"{type(exc).__name__}: {exc}", None)
            answer, body, cut = Access.UNAVAILABLE, b"", False
        else:
            self._stats.inc(f"robots.by_status.{response.status}")
            answer = access(response.status) if len(response.history) <= MAX_REDIRECTS else Access.UNAVAILABLE
            if answer is Access.UNREACHABLE:
                now = dt.datetime.now(dt.UTC)
                retry_after = retry_after_seconds(response.headers.get("Retry-After"), now=now)
                return self._unreachable(origin, state, request, f"status {response.status}", retry_after)
        if answer is Access.SUCCESSFUL:
            rules = RobotsRules.parse(body, self._user_agent, cut=cut)
        else:
            rules = RobotsRules.allowing_all()
        state.rules, state.refresh_at, state.unreachable = rules, self._clock() + CACHE_TTL, 0
        self._space(origin, host_slot(request), rules.crawl_delay)
        logger.debug("Fetched the robots.txt of %s: %s", origin, answer.value)
        return rules

    async def _download(self, robots: Request, call_next: Download) -> tuple[Response, bytes, bool]:
        """Fetch robots.txt, and read the first `PARSE_LIMIT` bytes of its body; return whether it had more."""
        streams = Streams(0)  # read as it arrives, and closed once the limit is reached
        try:
            with streaming_into(streams), following_at_most(MAX_REDIRECTS):
                response = await call_next(robots)
            transfer = streams.latest if response.streamed else None
            if transfer is None:
                return response, response.body[:PARSE_LIMIT], len(response.body) > PARSE_LIMIT
            body, cut = await self._read(transfer)
            return response, body, cut
        finally:
            await streams.close()

    async def _read(self, transfer: StreamedResponse) -> tuple[bytes, bool]:
        """Read a streamed body up to `PARSE_LIMIT` bytes; return whether it had more."""
        body = bytearray()
        async for chunk in transfer.iter_bytes():
            self._stats.inc("downloads.bytes", len(chunk))
            body += chunk
            if len(body) > PARSE_LIMIT:
                return bytes(body[:PARSE_LIMIT]), True
        return bytes(body), False

    def _unreachable(
        self, origin: str, state: _Origin, request: Request, why: str, retry_after: float | None
    ) -> RobotsRules | None:
        """Keep that an origin's robots.txt is unreachable; return the rules still in force, if any."""
        wait = min(UNREACHABLE_WAIT if retry_after is None else retry_after, self._cooldown_max)
        now = self._clock()
        if state.rules is not None:  # RFC 9309 §2.4 lets a crawler keep them while it is unreachable
            state.refresh_at = now + wait
            logger.info("The robots.txt of %s is unreachable (%s); the rules fetched before stay in force", origin, why)
            return state.rules
        state.unreachable += 1
        if state.unreachable >= UNREACHABLE_LIMIT:
            state.rules, state.refresh_at, state.unreachable = RobotsRules.disallowing_all(), now + CACHE_TTL, 0
            self._space(origin, host_slot(request), None)
            logger.warning(
                "The robots.txt of %s was unreachable %d times in a row (%s); its Requests are ignored until it is "
                "fetched again, in %d hours",
                origin,
                UNREACHABLE_LIMIT,
                why,
                CACHE_TTL // 3600,
            )
            return state.rules
        state.refresh_at = now + self._throttle.cool_down(host_slot(request), wait)
        logger.info(
            "The robots.txt of %s is unreachable (%s); the Requests to it wait %.0f s for it to be fetched again",
            origin,
            why,
            wait,
        )
        return None

    def _deferred(self, delay: float) -> NotYet:
        self._stats.inc("robots.deferred")
        return NotYet(delay, "robots.txt unreachable")

    def _space(self, origin: str, slot: str, crawl_delay: float | None) -> None:
        """Space the fetches of a domain by the longest `Crawl-delay` of its origins, at most `robots.crawl_delay_max`."""
        delays = self._delays.setdefault(slot, {})
        if crawl_delay:
            limit = self._crawl_delay_max
            delays[origin] = crawl_delay if limit is None else min(crawl_delay, limit)
        else:
            delays.pop(origin, None)
        self._throttle.space(slot, max(delays.values(), default=0.0))


def _origin(request: Request) -> str | None:
    """The origin of an http or https Request, whose robots.txt applies to it; `None` for other URLs."""
    if urlsplit(request.url).scheme not in _SCHEMES:
        return None
    return url_origin(request.url)


def _obeys(request: Request) -> bool:
    """Whether a Request obeys robots.txt: unless its `ext` says `{"robots": {"obey": False}}`."""
    options = request.ext.get("robots")
    return options is None or bool(options.get("obey", True))
