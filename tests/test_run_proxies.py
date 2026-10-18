"""The proxies of a Run: loading and refreshing them, what each fetch is given, and how it went."""

from __future__ import annotations

import asyncio
import itertools
import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Account, Request
from pernix.errors import ConnectError, ProxyError, SettingsError
from pernix.proxies import DefaultBindingPolicy, MemoryProxyPool, Proxy, ProxyCandidate
from pernix.proxies.proxy_sources import StaticProxies
from pernix.proxies.run_proxies import FollowedProxy, NoProxyYet, ProxySupply, RunProxies
from pernix.sending import Sending
from pernix.settings import ProxySettings
from pernix.stats import Stats
from pernix.testing import make_response

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from pernix.proxies import Binding, BindingPolicy, ProxyProvider

A, B = "http://user:a-secret@a.example:8080", "http://b.example:8080"
C = "http://c.example:8080"
NOW = 1_800_000_000.0


class Listed:
    """A provider giving the proxies set last, and failing once told to."""

    def __init__(self, *proxies: str | Proxy) -> None:
        self.given: Sequence[str | Proxy] = proxies
        self.error: Exception | None = None
        self.calls = 0
        self.called = asyncio.Event()

    async def proxies(self) -> Sequence[str | Proxy]:
        self.calls += 1
        self.called.set()
        if self.error is not None:
            raise self.error
        return self.given


def _count(stats: Stats, key: str, *, default: int | None = None) -> int:
    value = stats.get(key, default)
    assert isinstance(value, int)
    return value


class Watched(Stats):
    """Stats that tell when they change."""

    def __init__(self) -> None:
        super().__init__()
        self.changed = asyncio.Event()

    def inc(self, key: str, count: float = 1) -> None:
        super().inc(key, count)
        self.changed.set()


class Run:
    """What the proxies use of their Run: its stats, a stop, and the log masks."""

    def __init__(self) -> None:
        self.stats = Watched()
        self.stops = 0
        self.masked: set[str] = set()

    def stop(self) -> None:
        self.stops += 1

    def mask(self, secrets: Iterable[str]) -> None:
        self.masked |= set(secrets)

    def proxies(
        self,
        *sources: ProxyProvider,
        refresh: float | None = None,
        pool: MemoryProxyPool | None = None,
        policy: BindingPolicy | None = None,
        supply: ProxySupply | None = None,
    ) -> RunProxies:
        return RunProxies(
            MemoryProxyPool(stats=self.stats) if pool is None else pool,
            [(f"source {number}", source) for number, source in enumerate(sources, start=1)],
            policy=policy or DefaultBindingPolicy(ProxySettings()),
            refresh=refresh,
            stats=self.stats,
            stop=self.stop,
            mask=self.mask,
            supply=supply,
        )


async def test_the_proxies_of_every_source_are_used_together_once_each() -> None:
    run = Run()
    proxies = run.proxies(StaticProxies([A, "b.example:8080"]), Listed(B, "http://c.example:8080"))

    await proxies.open()
    taken = [await proxies.take() for _ in range(4)]
    await proxies.close()

    assert taken == [A, B, "http://c.example:8080", A]
    assert run.masked == {"a-secret"}


async def test_proxies_a_source_gave_that_are_not_valid_are_skipped_without_quoting_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run()
    proxies = run.proxies(Listed("user:secret-pw@no-port.example", 42, B))  # pyright: ignore[reportArgumentType]

    with caplog.at_level(logging.WARNING, logger="pernix"):
        await proxies.open()
    await proxies.close()

    assert await proxies.take() == B
    assert "source 1 gave that are not valid: #1 (a proxy needs a port), #2 (not a string or a Proxy)" in caplog.text
    assert "secret-pw" not in caplog.text


async def test_a_provider_may_give_records_to_tag_its_proxies(caplog: pytest.LogCaptureFixture) -> None:
    pool = MemoryProxyPool()
    given = [
        Proxy("A.example:1", tags=frozenset({"us"}), expires=NOW),
        Proxy(42),  # pyright: ignore[reportArgumentType]
        Proxy("c.example:3", tags=frozenset({3})),  # pyright: ignore[reportArgumentType]
        Proxy("user:secret-pw@no-port.example"),
    ]
    proxies = Run().proxies(Listed(*given), pool=pool)  # pyright: ignore[reportArgumentType]

    with caplog.at_level(logging.WARNING, logger="pernix"):
        await proxies.open()

    assert await pool.candidates() == (ProxyCandidate("http://a.example:1", tags=frozenset({"us"}), expires=NOW),)
    assert "#2 (Expected `str`, got `int` - at `$.url`)" in caplog.text
    assert "#3 (Expected `str`, got `int` - at `$.tags[0]`)" in caplog.text
    assert "#4 (a proxy needs a port)" in caplog.text
    assert "secret-pw" not in caplog.text


async def test_a_source_failing_when_the_run_starts_fails_it() -> None:
    source = Listed()
    source.error = SettingsError(["proxies.txt, line 2: a proxy needs a port"])

    with pytest.raises(SettingsError):
        await Run().proxies(source).open()


async def test_a_refresh_replaces_the_proxies_and_masks_the_new_ones() -> None:
    run = Run()
    source = Listed(B)
    proxies = run.proxies(source, refresh=0.01)
    await proxies.open()
    source.given = [A]
    source.called.clear()

    await asyncio.wait_for(source.called.wait(), timeout=5)
    await asyncio.sleep(0)  # the refresh lists them
    taken = await proxies.take()
    await proxies.close()

    assert taken == A
    assert run.masked == {"a-secret"}
    assert _count(run.stats, "proxies.refreshes") >= 1


async def test_a_refresh_that_fails_keeps_the_proxies(caplog: pytest.LogCaptureFixture) -> None:
    run = Run()
    source = Listed(B)
    proxies = run.proxies(source, refresh=0.01)
    await proxies.open()
    source.error = ConnectError("the API is down")

    with caplog.at_level(logging.ERROR, logger="pernix"):
        while source.calls < 3:
            source.called.clear()
            await asyncio.wait_for(source.called.wait(), timeout=5)
        await asyncio.sleep(0)
    taken = await proxies.take()
    await proxies.close()

    assert taken == B
    assert _count(run.stats, "proxies.refresh_failures") >= 1
    assert "Refreshing the proxies failed; the ones loaded before are kept" in caplog.text


async def test_a_fetch_waits_for_a_proxy_to_cool_down() -> None:
    pool = MemoryProxyPool(cooldown=30)
    proxies = Run().proxies(StaticProxies([B]), pool=pool)
    await proxies.open()
    await pool.report(B, failed=True, since=0)

    with pytest.raises(NoProxyYet) as waiting:
        await proxies.take()
    with pytest.raises(NoProxyYet):
        await proxies.bind("one", None)

    assert waiting.value.delay == pytest.approx(30, abs=1)
    assert waiting.value.reason == "no proxy available"


async def test_once_no_proxy_ever_will_be_available_the_run_is_stopped_once(caplog: pytest.LogCaptureFixture) -> None:
    run = Run()
    pool = MemoryProxyPool(max_failures=1)
    proxies = run.proxies(StaticProxies([B]), pool=pool)
    await proxies.open()
    await pool.report(B, failed=True, since=0)

    with caplog.at_level(logging.ERROR, logger="pernix"):
        for _ in range(3):
            with pytest.raises(NoProxyYet):
                await proxies.take()

    assert run.stops == 1
    assert "No proxy can be used: every proxy is retired or expired, and none are loaded again" in caplog.text


async def test_with_no_proxy_given_the_run_is_stopped(caplog: pytest.LogCaptureFixture) -> None:
    run = Run()
    proxies = run.proxies(Listed())
    await proxies.open()

    with caplog.at_level(logging.ERROR, logger="pernix"), pytest.raises(NoProxyYet):
        await proxies.bind("one", None)

    assert run.stops == 1
    assert "the sources gave none" in caplog.text


async def test_with_a_refresh_to_come_a_fetch_waits_for_it_instead() -> None:
    run = Run()
    proxies = run.proxies(Listed(), refresh=60)
    await proxies.open()

    with pytest.raises(NoProxyYet) as waiting:
        await proxies.take()
    await proxies.close()

    assert waiting.value.delay == pytest.approx(60, abs=1)
    assert run.stops == 0


class Recording(MemoryProxyPool):
    """A pool recording what is reported to it."""

    def __init__(self) -> None:
        super().__init__()
        self.reports: list[tuple[str, bool]] = []

    async def report(self, proxy: str, *, failed: bool, since: float) -> None:
        self.reports.append((proxy, failed))


def _got_response(sending: Sending) -> None:
    sending.received(make_response("https://site.example/"))


def _recorded_proxy_error(sending: Sending) -> None:
    sending.error = ProxyError("refused")


def _raised_proxy_error(_sending: Sending) -> None:
    raise ProxyError("refused")


def _raised_other_error(_sending: Sending) -> None:
    raise ConnectError("the site is down")


def _cancelled(_sending: Sending) -> None:
    raise asyncio.CancelledError


@pytest.mark.parametrize(
    ("fetch", "reports"),
    [
        (_got_response, [(B, False)]),
        (_recorded_proxy_error, [(B, True)]),
        (_raised_proxy_error, [(B, True)]),
        (_raised_other_error, []),
        (_cancelled, []),
    ],
    ids=["response", "proxy error recorded", "proxy error raised", "other error", "cancelled"],
)
async def test_how_a_fetch_went_through_its_proxy_is_reported(
    fetch: Callable[[Sending], None], reports: list[tuple[str, bool]]
) -> None:
    pool = Recording()
    proxies = Run().proxies(StaticProxies([B]), pool=pool)
    sending = Sending(Request("https://site.example/"))

    with suppress(ProxyError, ConnectError, asyncio.CancelledError):
        async with proxies.reporting(B, sending):
            fetch(sending)

    assert pool.reports == reports


class Told:
    """A binding policy keeping what it was told, and binding as the default does."""

    def __init__(self, rebind: str = "unavailable") -> None:
        self.told: list[Binding] = []
        self._default = DefaultBindingPolicy(ProxySettings(rebind=rebind))  # pyright: ignore[reportArgumentType]

    async def bind(self, binding: Binding) -> str | None:
        self.told.append(binding)
        return await self._default.bind(binding)


async def _opened(
    *urls: str, pool: MemoryProxyPool | None = None, policy: BindingPolicy | None = None, run: Run | None = None
) -> RunProxies:
    proxies = (run or Run()).proxies(StaticProxies(urls), pool=pool, policy=policy)
    await proxies.open()
    return proxies


async def test_by_default_a_session_keeps_its_proxy_and_sessions_share_one_only_when_they_must() -> None:
    proxies = await _opened(A, B)

    first = await proxies.bind("one", None)
    second = await proxies.bind("two", None)
    again = await proxies.bind("one", first)
    third = await proxies.bind("three", None)

    assert (first, second, again) == (A, B, A)
    assert third in (A, B)


async def test_leases_of_one_session_that_overlap_are_given_one_proxy() -> None:
    pool = MemoryProxyPool()
    proxies = await _opened(A, B, C, pool=pool)
    first = await proxies.bind("one", None)
    overlapping = await proxies.bind("one", None)  # a Lease that loaded the state before the first saved its binding
    await proxies.bind("two", None)

    await pool.report(A, failed=True, since=time.time())
    rebound = await proxies.bind("one", A)
    stale = await proxies.bind("one", A)  # another Lease of it, which loaded the state before the rebinding

    assert (first, overlapping) == (A, A)
    assert rebound == stale == C


@pytest.mark.parametrize(
    ("gone", "said"), [("cooldown", "is cooling down"), ("retired", "is retired"), ("unlisted", "is not listed")]
)
async def test_a_session_whose_proxy_is_gone_is_bound_to_another(
    gone: str, said: str, caplog: pytest.LogCaptureFixture
) -> None:
    pool = MemoryProxyPool(max_failures=1 if gone == "retired" else 5)
    policy = Told()
    proxies = await _opened(A, B, pool=pool, policy=policy)
    await proxies.bind("one", None)
    if gone == "unlisted":
        await pool.replace([Proxy(B)])
    else:
        await pool.report(A, failed=True, since=time.time())

    with caplog.at_level(logging.INFO, logger="pernix"):
        rebound = await proxies.bind("one", A)

    assert rebound == B
    assert policy.told[-1].reason == gone
    assert f"The proxy of the Session 'one' {said}; it goes through another" in caplog.text


async def test_the_policy_is_told_of_the_session_its_account_its_proxy_and_the_candidates() -> None:
    policy = Told()
    account = Account(username="alice", password="pw")  # pyright: ignore[reportArgumentType]
    proxies = await _opened(A, B, policy=policy)

    await proxies.bind("one", None, account=account)
    await proxies.bind("one", A)
    await proxies.bind("two", "http://gone.example:1")  # its state names a proxy no longer listed

    new, lease, unlisted = policy.told
    assert (new.session, new.account, new.current, new.health, new.reason) == ("one", account, None, None, "new")
    assert new.candidates == (ProxyCandidate(A), ProxyCandidate(B))
    assert (lease.account, lease.current, lease.health, lease.reason) == (None, A, "available", "lease")
    assert lease.candidates == (ProxyCandidate(B), ProxyCandidate(A, sessions=frozenset({"one"})))
    assert (unlisted.current, unlisted.health, unlisted.reason) == ("http://gone.example:1", None, "unlisted")


async def test_while_no_proxy_is_available_a_session_the_policy_binds_to_none_waits_for_one() -> None:
    pool = MemoryProxyPool(cooldown=30)
    run = Run()
    policy = Told()
    proxies = await _opened(B, pool=pool, policy=policy, run=run)
    await pool.report(B, failed=True, since=time.time())

    with pytest.raises(NoProxyYet, match="no proxy available") as waiting:
        await proxies.bind("one", None)

    assert [binding.reason for binding in policy.told] == ["new"]  # asked, as it may give a proxy of its own
    assert waiting.value.delay == pytest.approx(30, abs=1)
    assert run.stats.get("proxies.binding_waits") is None


async def test_sessions_no_proxy_fits_wait_counted_and_logged_once_until_bound(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run()
    proxies = await _opened(A, B, pool=MemoryProxyPool(max_sessions=1), run=run)
    await proxies.bind("one", None)
    await proxies.bind("two", None)

    with caplog.at_level(logging.WARNING, logger="pernix"):
        waits: list[NoProxyYet] = []
        for _ in range(2):
            with pytest.raises(NoProxyYet) as waiting:
                await proxies.bind("three", None)
            waits.append(waiting.value)
        await proxies.release("one", A)  # its Lease ended
        await proxies.unbind("one")
        bound = await proxies.bind("three", None)

    assert [(wait.delay, wait.reason) for wait in waits] == [(1.0, "no proxy fits the Session")] * 2
    assert caplog.text.count("No proxy fits the Session 'three' now; its Requests wait for one") == 1
    assert _count(run.stats, "proxies.binding_waits") == 2
    assert bound == A


async def test_with_every_lease_a_session_goes_through_the_proxies_with_room_in_turn() -> None:
    every_lease = DefaultBindingPolicy(ProxySettings(rebind="every_lease"))
    proxies = await _opened(A, B, C, pool=MemoryProxyPool(max_sessions=1), policy=every_lease)
    await proxies.bind("two", None)  # A, which it keeps

    seen: list[str] = []
    for _ in range(4):
        seen.append(await proxies.bind("one", seen[-1] if seen else None))

    assert seen == [B, C, B, C]


class Racing:
    """A binding policy binding as the default does, once the first two Sessions read the same candidates."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self._both_read = asyncio.Barrier(2)
        self._default = DefaultBindingPolicy(ProxySettings())

    async def bind(self, binding: Binding) -> str | None:
        self.asked.append(binding.session)
        if len(self.asked) <= 2:
            await self._both_read.wait()
        return await self._default.bind(binding)


async def test_a_session_whose_pick_another_took_first_is_bound_as_the_policy_picks_again() -> None:
    policy = Racing()
    proxies = await _opened(A, B, pool=MemoryProxyPool(max_sessions=1), policy=policy)

    bound = await asyncio.gather(proxies.bind("one", None), proxies.bind("two", None))

    assert sorted(bound) == sorted([A, B])  # both picked A first
    assert len(policy.asked) == 3


class Stubborn:
    """A binding policy that always picks one proxy, or whatever it was given."""

    def __init__(self, proxy: Any) -> None:
        self.proxy = proxy
        self.asked = 0

    async def bind(self, binding: Binding) -> Any:
        self.asked += 1
        return self.proxy


async def test_the_policy_is_asked_again_only_a_few_times() -> None:
    policy = Stubborn(A)
    proxies = await _opened(A, B, pool=MemoryProxyPool(max_sessions=1), policy=policy)
    await proxies.bind("one", None)

    with pytest.raises(NoProxyYet, match="no proxy fits the Session"):
        await proxies.bind("two", None)

    assert policy.asked == 1 + 3


async def test_a_policy_that_picks_a_proxy_the_pool_does_not_list_fails_the_binding() -> None:
    proxies = await _opened(A, policy=Stubborn("http://user:pw@elsewhere.example:1"))

    with pytest.raises(ValueError, match="picked a proxy the Proxy Pool does not list") as failed:
        await proxies.bind("one", None)

    assert "pw" not in str(failed.value)


async def test_a_policy_that_raises_fails_the_binding() -> None:
    class Broken:
        async def bind(self, binding: Binding) -> str | None:
            raise RuntimeError("the database is down")

    proxies = await _opened(A, policy=Broken())

    with pytest.raises(RuntimeError, match="the database is down"):
        await proxies.bind("one", None)


async def test_a_session_kept_on_a_proxy_that_cools_down_waits_for_it(caplog: pytest.LogCaptureFixture) -> None:
    pool = MemoryProxyPool(cooldown=30)
    proxies = await _opened(A, B, pool=pool, policy=Told("retired"))
    await proxies.bind("one", None)
    await pool.report(A, failed=True, since=time.time())

    with caplog.at_level(logging.INFO, logger="pernix"), pytest.raises(FollowedProxy) as following:
        await proxies.bind("one", A)

    assert following.value.until == pytest.approx(time.time() + 30, abs=1)
    assert following.value.delay == pytest.approx(30, abs=1)
    assert {candidate.url: candidate.sessions for candidate in await pool.candidates()}[A] == {"one"}  # still its
    assert "The proxy of the Session 'one' is cooling down; the Session waits for it" in caplog.text


@pytest.mark.parametrize(("gone", "said"), [("retired", "is retired"), ("unlisted", "is not listed")])
async def test_a_session_kept_on_a_proxy_that_is_gone_is_retired_with_it(
    gone: str, said: str, caplog: pytest.LogCaptureFixture
) -> None:
    pool = MemoryProxyPool(max_failures=1)
    proxies = await _opened(A, B, pool=pool, policy=Told("never"))
    await proxies.bind("one", None)
    if gone == "unlisted":
        await pool.replace([Proxy(B)])
    else:
        await pool.report(A, failed=True, since=time.time())

    with caplog.at_level(logging.WARNING, logger="pernix"), pytest.raises(FollowedProxy) as following:
        await proxies.bind("one", A)

    assert following.value.until is None
    assert f"The proxy of the Session 'one' {said}; the Session is retired with it" in caplog.text


@pytest.mark.parametrize(("rebind", "follows"), [("unavailable", False), ("retired", True)])
async def test_once_a_fetch_through_its_proxy_failed_the_policy_tells_whether_its_session_follows_it(
    rebind: str, *, follows: bool
) -> None:
    pool = MemoryProxyPool(cooldown=30)
    policy = Told(rebind)
    proxies = await _opened(A, B, pool=pool, policy=policy)
    await proxies.bind("one", None)

    while_available = await proxies.following("one", A)
    asked = len(policy.told)
    await pool.report(A, failed=True, since=time.time())
    followed = await proxies.following("one", A)

    assert while_available is None
    assert asked == 1  # not asked while the proxy is available
    assert (followed is not None) == follows
    assert policy.told[-1].reason == "cooldown"
    if followed is not None:
        assert followed.until == pytest.approx(time.time() + 30, abs=1)


OWN = "http://alice:own-secret@own.example:9000"


class OwnFor:
    """A binding policy giving each Session it knows a proxy of its own, and the others none."""

    def __init__(self, **own: str) -> None:
        self.own = own
        self.told: list[Binding] = []

    async def bind(self, binding: Binding) -> Proxy | None:
        self.told.append(binding)
        url = self.own.get(binding.session)
        return None if url is None else Proxy(url, tags=frozenset({"isp"}))


async def test_a_policy_may_give_a_session_a_proxy_of_its_own_held_for_it_alone() -> None:
    run = Run()
    policy = OwnFor(one=OWN)
    proxies = await _opened(B, policy=policy, run=run)

    bound = await proxies.bind("one", None)
    again = await proxies.bind("one", bound)
    taken = [await proxies.take() for _ in range(2)]

    assert bound == again == OWN
    assert "own-secret" in run.masked
    assert taken == [B, B]  # it takes no turns
    lease = policy.told[-1]
    assert (lease.reason, lease.current, lease.health) == ("lease", OWN, "available")
    assert [(candidate.url, candidate.tags, candidate.own) for candidate in lease.candidates] == [
        (B, frozenset(), False),
        (OWN, frozenset({"isp"}), True),
    ]


@pytest.mark.parametrize(
    ("given", "error", "message"),
    [
        (
            Proxy("http://alice:own-secret@own.example"),
            ValueError,
            "of its own that is not valid: a proxy needs a port",
        ),
        (Proxy(B), ValueError, "the Session 'one' as its own a proxy the Proxy Pool lists"),
        (OWN.encode(), TypeError, "must return a proxy URL, a Proxy, or None, not bytes"),
    ],
    ids=["not valid", "listed", "neither"],
)
async def test_a_policy_giving_a_proxy_of_its_own_that_is_not_valid_or_listed_fails_the_binding(
    given: object, error: type[Exception], message: str
) -> None:
    proxies = await _opened(B, policy=Stubborn(given))

    with pytest.raises(error, match=message) as failed:
        await proxies.bind("one", None)

    assert "own-secret" not in str(failed.value)


async def test_a_session_given_its_own_proxy_again_follows_it_as_any_other() -> None:
    pool = MemoryProxyPool(cooldown=30, max_failures=2)
    proxies = await _opened(B, pool=pool, policy=OwnFor(one=OWN))
    await proxies.bind("one", None)

    await pool.report(OWN, failed=True, since=time.time())
    with pytest.raises(FollowedProxy) as cooling:
        await proxies.bind("one", OWN)
    await pool.report(OWN, failed=True, since=time.time())
    with pytest.raises(FollowedProxy) as retired:
        await proxies.bind("one", OWN)

    assert cooling.value.until == pytest.approx(time.time() + 30, abs=1)
    assert retired.value.until is None


async def test_without_a_source_sessions_go_through_the_proxies_the_policy_gives_them(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run()
    proxies = run.proxies(policy=OwnFor(one=OWN))

    with caplog.at_level(logging.INFO, logger="pernix"):
        await proxies.open()
        bound = await proxies.bind("one", None)
        with pytest.raises(NoProxyYet) as waiting:
            await proxies.bind("two", None)

    assert bound == OWN
    assert (waiting.value.delay, waiting.value.reason) == (1.0, "no proxy fits the Session")
    assert _count(run.stats, "proxies.binding_waits") == 1
    assert run.stops == 0
    assert "No source of proxies is set; Sessions go through those the binding policy gives them" in caplog.text


async def test_the_run_drains_for_want_of_proxies_once_no_session_has_one_of_its_own_either() -> None:
    run = Run()
    pool = MemoryProxyPool(max_failures=1)
    proxies = await _opened(B, pool=pool, policy=OwnFor(one=OWN), run=run)
    await proxies.bind("one", None)
    await pool.report(B, failed=True, since=time.time())  # the only proxy listed is Retired

    with pytest.raises(NoProxyYet) as meanwhile:
        await proxies.bind("two", None)
    stops = run.stops
    await pool.report(OWN, failed=True, since=time.time())
    with pytest.raises(NoProxyYet) as exhausted:
        await proxies.bind("two", None)

    assert (meanwhile.value.delay, stops) == (1.0, 0)  # "one" goes on through its own
    assert (exhausted.value.delay, run.stops) == (5.0, 1)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> float:
        return self.now


class Extracting:
    """An extraction API of one's own: each call gives as many new proxies as wanted, until it runs dry."""

    def __init__(self) -> None:
        self.asked: list[int] = []
        self.dry = False
        self._made = itertools.count(1)

    async def proxies(self, wanted: int = 1) -> list[Proxy]:
        self.asked.append(wanted)
        if self.dry:
            return []
        return [Proxy(f"http://p{next(self._made)}.example:8080") for _ in range(wanted)]


def _extracting(**options: float) -> ProxySupply:
    return ProxySupply(add=True, extracting=frozenset({"source 1"}), interval=0, **options)  # pyright: ignore[reportArgumentType]


async def _extracted(run: Run, extractions: int) -> None:
    """Wait for the extractions under way to end, until `extractions` of them are counted."""
    async with asyncio.timeout(5):
        while _count(run.stats, "proxies.extractions", default=0) < extractions:
            run.stats.changed.clear()
            await run.stats.changed.wait()
    await asyncio.sleep(0)  # the extraction ends


async def test_with_add_each_load_adds_the_proxies_and_a_session_keeps_its_own() -> None:
    run = Run()
    source = Listed(B)
    pool = MemoryProxyPool()
    proxies = run.proxies(source, refresh=0.01, pool=pool, supply=_extracting())
    await proxies.open()
    bound = await proxies.bind("one", None)
    source.given = [C]
    source.called.clear()

    await asyncio.wait_for(source.called.wait(), timeout=5)
    await asyncio.sleep(0)  # the refresh adds them
    again = await proxies.bind("one", bound)
    listed = {candidate.url for candidate in await pool.candidates()}
    await proxies.close()

    assert (bound, again) == (B, B)
    assert listed == {B, C}
    assert _count(run.stats, "proxies.extractions") >= 2


@pytest.mark.parametrize("rebind", ["unavailable", "never"])
async def test_a_session_whose_proxy_expired_is_bound_as_its_policy_says(
    rebind: str, caplog: pytest.LogCaptureFixture
) -> None:
    clock = Clock()
    pool = MemoryProxyPool(clock=clock)
    policy = Told(rebind)
    proxies = Run().proxies(StaticProxies([Proxy(A, expires=NOW + 10), Proxy(B)]), pool=pool, policy=policy)
    await proxies.open()
    first = await proxies.bind("one", None)
    clock.now += 10

    with caplog.at_level(logging.INFO, logger="pernix"):
        if rebind == "never":
            with pytest.raises(FollowedProxy) as following:
                await proxies.bind("one", first)
            assert following.value.until is None
        else:
            assert await proxies.bind("one", first) == B

    assert first == A
    assert policy.told[-1].reason == "expired"
    assert "The proxy of the Session 'one' has expired" in caplog.text


async def test_on_demand_proxies_are_extracted_below_the_level_within_the_limit_then_the_run_drains(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run = Run()
    source = Extracting()
    pool = MemoryProxyPool(max_failures=1, stats=run.stats)
    proxies = run.proxies(source, pool=pool, supply=_extracting(min_available=2, max_extracted=5))

    async def retire(*urls: str) -> None:
        for url in urls:
            await pool.report(url, failed=True, since=time.time())

    with caplog.at_level(logging.WARNING, logger="pernix"):
        await proxies.open()  # 2 wanted, to reach the level
        first = await proxies.take()
        await retire(first)
        await proxies.take()  # one available, below the level
        await _extracted(run, 2)
        available = {candidate.url for candidate in await pool.candidates() if candidate.health == "available"}
        await retire(*available)
        with pytest.raises(NoProxyYet) as waiting:
            await proxies.take()  # none available: 2 wanted, as many as the limit leaves
        await _extracted(run, 3)
        await retire(*[candidate.url for candidate in await pool.candidates() if candidate.health == "available"])
        stops_before = run.stops
        with pytest.raises(NoProxyYet):
            await proxies.take()

    assert source.asked == [2, 1, 2]
    assert len(available) == 2
    assert waiting.value.delay == pytest.approx(0.5)
    assert (stops_before, run.stops) == (0, 1)
    assert _count(run.stats, "proxies.extracted") == 5
    assert "The sources extracted 5 proxies, reaching the limit; no more are extracted" in caplog.text


async def test_the_run_drains_once_extractions_in_a_row_gave_no_proxy(caplog: pytest.LogCaptureFixture) -> None:
    run = Run()
    source = Extracting()
    source.dry = True
    proxies = run.proxies(source, supply=_extracting(min_available=1, attempts=3))

    with caplog.at_level(logging.WARNING, logger="pernix"):
        await proxies.open()
        for extractions in (2, 3):
            with pytest.raises(NoProxyYet):
                await proxies.take()
            await _extracted(run, extractions)
        stops_before = run.stops
        with pytest.raises(NoProxyYet):
            await proxies.take()

    assert (stops_before, run.stops) == (0, 1)
    assert len(source.asked) == 3
    assert "3 extractions in a row gave no proxy; no more are made on demand" in caplog.text
    assert "and no more can be extracted" in caplog.text


async def test_a_session_no_proxy_fits_has_one_more_extracted() -> None:
    run = Run()
    source = Extracting()
    proxies = run.proxies(source, pool=MemoryProxyPool(max_sessions=1), supply=_extracting(min_available=1))
    await proxies.open()
    first = await proxies.bind("one", None)

    with pytest.raises(NoProxyYet, match="no proxy fits the Session"):
        await proxies.bind("two", None)
    await _extracted(run, 2)
    second = await proxies.bind("two", None)

    assert (first, second) == ("http://p1.example:8080", "http://p2.example:8080")
    assert source.asked == [1, 1]


async def test_a_provider_that_takes_no_wanted_is_asked_as_before() -> None:
    run = Run()
    source = Listed(B)
    proxies = run.proxies(source, supply=_extracting(min_available=3))

    await proxies.open()

    assert await proxies.take() == B
    assert source.calls >= 1
