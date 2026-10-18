"""The proxies of a Run: loading and refreshing them, what each fetch is given, and how it went."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import ConnectError, ProxyError, SettingsError
from pernix.proxies import MemoryProxyPool
from pernix.proxies.proxy_sources import StaticProxies
from pernix.proxies.run_proxies import NoProxyYet, RunProxies
from pernix.sending import Sending
from pernix.stats import Stats
from pernix.testing import make_response

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from pernix.proxies import ProxyProvider

A, B = "http://user:a-secret@a.example:8080", "http://b.example:8080"


class Listed:
    """A provider giving the proxies set last, and failing once told to."""

    def __init__(self, *proxies: str) -> None:
        self.given: Sequence[str] = proxies
        self.error: Exception | None = None
        self.calls = 0
        self.called = asyncio.Event()

    async def proxies(self) -> Sequence[str]:
        self.calls += 1
        self.called.set()
        if self.error is not None:
            raise self.error
        return self.given


def _count(stats: Stats, key: str) -> int:
    value = stats.get(key)
    assert isinstance(value, int)
    return value


class Run:
    """What the proxies use of their Run: its stats, a stop, and the log masks."""

    def __init__(self) -> None:
        self.stats = Stats()
        self.stops = 0
        self.masked: set[str] = set()

    def stop(self) -> None:
        self.stops += 1

    def mask(self, secrets: Iterable[str]) -> None:
        self.masked |= set(secrets)

    def proxies(
        self, *sources: ProxyProvider, refresh: float | None = None, pool: MemoryProxyPool | None = None
    ) -> RunProxies:
        return RunProxies(
            MemoryProxyPool(stats=self.stats) if pool is None else pool,
            [(f"source {number}", source) for number, source in enumerate(sources, start=1)],
            refresh=refresh,
            stats=self.stats,
            stop=self.stop,
            mask=self.mask,
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
    assert "source 1 gave that are not valid: #1 (a proxy needs a port), #2 (not a string)" in caplog.text
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
    assert "No proxy can be used: every proxy is retired, and none are loaded again; stopping the Run" in caplog.text


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
