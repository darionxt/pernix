"""Sending independent Requests: as they are, or each attempt through the Run's next proxy."""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import Blocked, ProxyError, SessionError
from pernix.proxies import DefaultBindingPolicy, MemoryProxyPool
from pernix.proxies.proxy_sources import StaticProxies
from pernix.proxies.run_proxies import NoProxyYet, RunProxies
from pernix.sending import IndependentSending
from pernix.settings import ProxySettings
from pernix.stats import Stats
from pernix.testing import make_response

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix import Response

SITE = "https://site.example"
A, B = "http://a.example:8080", "http://b.example:8080"


async def _proxies(*urls: str, pool: MemoryProxyPool | None = None) -> RunProxies:
    def mask(_: Iterable[str]) -> None:
        pass

    proxies = RunProxies(
        MemoryProxyPool() if pool is None else pool,
        [("proxies.urls", StaticProxies(urls))],
        policy=DefaultBindingPolicy(ProxySettings()),
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


def _forbidden(request: Request) -> Response:
    return make_response(request.url, status=403, request=request)


@pytest.mark.parametrize(
    ("proxied", "render_blocked", "request_", "judged"),
    [
        (True, False, Request(f"{SITE}/"), True),  # the next attempt goes through another proxy
        (False, True, Request(f"{SITE}/"), True),  # it is retried rendered
        (False, True, Request(f"{SITE}/", fetcher="http"), False),  # it names its Fetcher, so is not rendered
        (False, False, Request(f"{SITE}/"), False),  # nothing would change
        (True, False, Request(f"{SITE}/", proxy="http://own.example:1"), False),  # it goes through its own
        (None, False, Request(f"{SITE}/"), False),  # the Run has a single proxy, the next attempt goes through
    ],
    ids=["proxied", "rendered", "fetcher named", "nothing to change", "a proxy of its own", "a single proxy"],
)
async def test_without_a_session_a_response_is_judged_blocked_only_when_a_retry_can_change_something(
    request_: Request, *, proxied: bool | None, render_blocked: bool, judged: bool
) -> None:
    proxies = await _proxies(A, B) if proxied else await _proxies(A) if proxied is None else None
    sender = IndependentSending(
        proxies, is_blocked=lambda response: response.status == 403, render_blocked=render_blocked
    )

    async with sender.sending(request_, wait=0) as sending:
        if judged:
            with pytest.raises(Blocked, match="the Response is blocked"):
                sending.received(_forbidden(sending.request))
        else:
            sending.received(_forbidden(sending.request))

    assert isinstance(sending.error, Blocked) == judged
