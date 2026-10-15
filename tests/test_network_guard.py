"""The network guard that keeps Fetchers on loopback hosts, as in tests."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import NetworkBlocked
from pernix.fetchers import HTTPFetcher
from pernix.fetchers.network_guard import NetworkGuard, blocking_network, current_guard, is_loopback_host

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

PUBLIC = "http://192.0.2.1/"  # TEST-NET-1: never routed, so a broken guard times out rather than reaches a host


@pytest.mark.parametrize(
    ("host", "loopback"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("LocalHost.", True),
        ("shop.localhost", True),
        ("localhost.example", False),
        ("192.0.2.1", False),
        ("0.0.0.0", False),  # noqa: S104  # an address, not a binding
        ("example.com", False),
        ("", False),
    ],
)
def test_loopback_hosts(host: str, loopback: bool) -> None:
    assert is_loopback_host(host) is loopback


def test_the_guard_admits_loopback_urls_and_loopback_proxies() -> None:
    guard = NetworkGuard()

    assert guard.admits("http://127.0.0.1:8080/page")
    assert guard.admits("https://example.com/", proxy="http://127.0.0.1:3128")
    assert guard.admits("https://example.com/", proxy="localhost:3128")
    assert not guard.admits("https://example.com/")
    assert not guard.admits("http://127.0.0.1/", proxy="http://proxy.example:3128")
    with pytest.raises(NetworkBlocked, match=r"refused https://example\.com/: only loopback hosts"):
        guard.check("https://example.com/")
    assert guard.blocked == ["https://example.com/"]


def test_guards_nest_and_end_with_their_block() -> None:
    outer_before = current_guard()
    with blocking_network() as outer:
        assert current_guard() is outer
        with blocking_network() as inner:
            assert current_guard() is inner
        assert current_guard() is outer
    assert current_guard() is outer_before


@pytest.mark.parametrize("url", [PUBLIC, "https://pernix.invalid/"])
async def test_the_http_fetcher_refuses_other_hosts_without_connecting(url: str) -> None:
    async with HTTPFetcher() as fetcher:
        with blocking_network() as guard:
            started = time.monotonic()
            with pytest.raises(NetworkBlocked):
                await fetcher.fetch(Request(url))
            with pytest.raises(NetworkBlocked):
                async with fetcher.stream(Request(url)):
                    pass

    assert time.monotonic() - started < 1
    assert guard.blocked == [url, url]


async def test_redirects_to_other_hosts_are_refused_before_curl_follows_them(server: LoopbackServer) -> None:
    server.route("/away", status=302, headers=[("Location", PUBLIC + "page")])
    server.route("/near", status=301, headers=[("Location", "/home")])
    server.route("/home", "home")

    async with HTTPFetcher() as fetcher:
        with blocking_network() as guard:
            started = time.monotonic()
            with pytest.raises(NetworkBlocked, match=r"refused the redirect from http://127\.0\.0\.1:\d+/away to"):
                await fetcher.fetch(Request(server.url("/away")))
            near = await fetcher.fetch(Request(server.url("/near")))

    assert time.monotonic() - started < 1
    assert guard.blocked == [PUBLIC + "page"]
    assert (near.url, near.body) == (server.url("/home"), b"home")


async def test_a_loopback_proxy_takes_requests_to_any_host(server: LoopbackServer) -> None:
    server.route("/", "proxied")

    async with HTTPFetcher() as fetcher:
        with blocking_network() as guard:
            response = await fetcher.fetch(Request("http://shop.example/", proxy=server.base_url))

    assert response.body == b"proxied"
    assert guard.blocked == []
