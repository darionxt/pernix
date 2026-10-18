"""Sending independent Requests: as they are, or each attempt through the Run's next proxy."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import ProxyError, SessionError
from pernix.proxies import MemoryProxyPool
from pernix.proxies.proxy_sources import StaticProxies
from pernix.proxies.run_proxies import NoProxyYet, RunProxies
from pernix.sending import IndependentSending
from pernix.stats import Stats

if TYPE_CHECKING:
    from collections.abc import Iterable

SITE = "https://site.example"
A, B = "http://a.example:8080", "http://b.example:8080"


async def _proxies(*urls: str, pool: MemoryProxyPool | None = None) -> RunProxies:
    def mask(_: Iterable[str]) -> None:
        pass

    proxies = RunProxies(
        MemoryProxyPool() if pool is None else pool,
        [("proxies.urls", StaticProxies(urls))],
        refresh=None,
        stats=Stats(),
        stop=lambda: None,
        mask=mask,
    )
    await proxies.open()
    return proxies


async def test_without_proxies_requests_go_as_they_are() -> None:
    async with IndependentSending().sending(Request(f"{SITE}/"), wait=0) as sending:
        assert (sending.session, sending.request.session, sending.request.proxy) == (None, None, None)


async def test_a_request_naming_a_session_is_refused_without_a_session_pool() -> None:
    with pytest.raises(SessionError, match="no Session Pool is enabled"):
        async with IndependentSending().sending(Request(f"{SITE}/", session="a"), wait=0):
            pass


async def test_each_attempt_goes_through_the_next_proxy() -> None:
    sender = IndependentSending(await _proxies(A, B))
    request = Request(f"{SITE}/")
    proxies: list[str | None] = []

    for _ in range(3):
        async with sender.sending(request, wait=0) as sending:
            proxies.append(sending.request.proxy)

    assert proxies == [A, B, A]
    assert request.proxy is None


async def test_a_proxy_that_failed_is_skipped_by_the_next_attempt() -> None:
    sender = IndependentSending(await _proxies(A, B))
    request = Request(f"{SITE}/")

    with pytest.raises(ProxyError):
        async with sender.sending(request, wait=0):
            raise ProxyError("refused")
    taken = []
    for _ in range(2):
        async with sender.sending(request, wait=0) as sending:
            taken.append(sending.request.proxy)

    assert taken == [B, B]


@pytest.mark.parametrize("own", ["http://own.example:1", ""], ids=["its own", "none"])
async def test_a_request_with_a_proxy_of_its_own_goes_through_it_and_is_not_reported(own: str) -> None:
    pool = MemoryProxyPool(max_failures=1)
    sender = IndependentSending(await _proxies(A, pool=pool))

    sent: list[str | None] = []

    with suppress(ProxyError):
        async with sender.sending(Request(f"{SITE}/", proxy=own), wait=0) as sending:
            sent.append(sending.request.proxy)
            raise ProxyError("refused")

    assert sent == [own]
    assert pool.health(A) == "available"


async def test_with_no_proxy_available_the_request_waits() -> None:
    pool = MemoryProxyPool(cooldown=30)
    sender = IndependentSending(await _proxies(A, pool=pool))
    await pool.report(A, failed=True, since=0)

    with pytest.raises(NoProxyYet):
        async with sender.sending(Request(f"{SITE}/"), wait=0):
            pass
