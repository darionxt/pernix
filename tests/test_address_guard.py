"""The address guard of `network.public_only`, which keeps untrusted links off private addresses."""

from __future__ import annotations

import ipaddress
import re
import socket
import time
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import urlsplit

import msgspec
import pytest

from pernix import Request, Response, Spider
from pernix.errors import ConnectError, NetworkBlocked, ProxyError, SettingsError, Timeout
from pernix.fetchers import BROWSER_FETCHER, FetcherDispatch, HTTPFetcher
from pernix.fetchers.address_guard import AddressGuard, PinnedHost, is_public_address
from pernix.settings import CORE_SECTIONS, NetworkSettings, load_settings
from pernix.testing import FakeFetcher, crawl, make_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping
    from pathlib import Path

    from pernix.testing import LoopbackServer

GUARDED = NetworkSettings(public_only=True)


@pytest.fixture
def resolver(monkeypatch: pytest.MonkeyPatch) -> Callable[[Mapping[str, list[str]]], None]:
    """Answer the host names given with their addresses, and no other, without asking DNS."""

    def answer(addresses: Mapping[str, list[str]]) -> None:
        def getaddrinfo(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[Any]:
            if host not in addresses:
                raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
            return [
                (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in addresses[host]
            ]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    return answer


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True),
        ("2606:2800:21f:cb07:6820:80da:af6b:8b2c", True),
        ("::ffff:93.184.215.14", True),
        ("64:ff9b::5db8:d70e", True),  # NAT64 for 93.184.215.14
        ("127.0.0.1", False),
        ("10.1.2.3", False),
        ("172.16.0.1", False),
        ("192.168.1.1", False),
        ("169.254.169.254", False),
        ("100.64.0.1", False),
        ("0.0.0.0", False),  # noqa: S104  # an address, not a binding
        ("192.0.2.1", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("::1", False),
        ("::", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("ff02::1", False),
        ("::ffff:127.0.0.1", False),
        ("64:ff9b::a01:203", False),  # NAT64 for 10.1.2.3
    ],
)
def test_public_addresses(address: str, public: bool) -> None:
    assert is_public_address(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://127.0.0.1:8080/", "127.0.0.1 is a loopback address"),
        ("http://[::1]/", "::1 is a loopback address"),
        ("http://169.254.169.254/latest/meta-data/", "169.254.169.254 is a link-local address"),
        ("http://10.0.0.5/", "10.0.0.5 is a private address"),
        ("http://0.0.0.0/", "0.0.0.0 is an unspecified address"),
        ("http://100.64.0.1/", "100.64.0.1 is an address that is not publicly routable"),
        ("http://127.1/", "127.1, which is 127.0.0.1, is a loopback address"),  # as curl and browsers read it
        ("http://2130706433/", "2130706433, which is 127.0.0.1, is a loopback address"),
        ("http://0x7f000001/", "0x7f000001, which is 127.0.0.1, is a loopback address"),
        ("http://10.0.0.256/", "10.0.0.256 is not a valid host"),
        ("http://localhost/", "localhost is a loopback host"),
        ("http://admin.localhost/", "admin.localhost is a loopback host"),
    ],
)
async def test_hosts_that_are_not_public_are_refused(url: str, reason: str, refused_proxy: str) -> None:
    refusal = re.escape(f"network.public_only refused {url}: {reason}") + "$"

    with pytest.raises(NetworkBlocked, match=refusal):
        await AddressGuard().check(url)
    with pytest.raises(NetworkBlocked, match=refusal):  # a proxy would reach them too
        await AddressGuard().check(url, proxy=refused_proxy)


async def test_names_are_refused_when_any_address_they_resolve_to_is_not_public(
    resolver: Callable[[Mapping[str, list[str]]], None],
) -> None:
    resolver({"mixed.example": ["93.184.215.14", "10.0.0.5"], "public.example": ["93.184.215.14", "2606:2800::1"]})

    with pytest.raises(NetworkBlocked, match=r"mixed\.example resolves to 10\.0\.0\.5, a private address$"):
        await AddressGuard().check("https://mixed.example/")
    pinned = await AddressGuard().check("https://public.example/")
    assert pinned == PinnedHost("public.example", 443, ("93.184.215.14", "2606:2800::1"))


async def test_allowed_hosts_proxies_and_public_addresses_are_not_resolved(
    resolver: Callable[[Mapping[str, list[str]]], None],
) -> None:
    resolver({})
    guard = AddressGuard(frozenset({"wiki.internal", "localhost"}))

    assert await guard.check("http://wiki.internal/") is None
    assert await guard.check("http://localhost:8080/") is None
    assert await guard.check("http://93.184.215.14/") is None
    assert await guard.check("http://unknown.example/", proxy="http://127.0.0.1:3128") is None
    with pytest.raises(NetworkBlocked):
        await guard.check("http://10.0.0.5/", proxy="http://127.0.0.1:3128")  # an address the proxy would reach


async def test_a_name_that_does_not_resolve_is_a_connect_error(
    resolver: Callable[[Mapping[str, list[str]]], None],
) -> None:
    resolver({})

    with pytest.raises(ConnectError, match=r"could not resolve nothing\.example") as caught:
        await AddressGuard().check("https://nothing.example/")
    assert caught.value.code == 6


def test_allowed_hosts_are_host_names() -> None:
    settings = NetworkSettings(allow_hosts=("Wiki.Internal", "[::1]", "faß.example"))

    assert settings.allow_hosts == ("wiki.internal", "::1", "xn--fa-hia.example")
    with pytest.raises(msgspec.ValidationError, match=r"'https://wiki\.internal' is not a host name"):
        msgspec.convert({"allow_hosts": ["https://wiki.internal"]}, NetworkSettings)
    with pytest.raises(SettingsError, match=r"network\.allow_hosts: 'wiki\.internal:8080' is not a host name"):
        load_settings(sections=CORE_SECTIONS, environ={}, overrides={"network.allow_hosts": ["wiki.internal:8080"]})


async def test_the_http_fetcher_refuses_loopback_hosts_without_connecting(server: LoopbackServer) -> None:
    server.route("/page", "page")

    async with HTTPFetcher(network=GUARDED) as fetcher:
        with pytest.raises(NetworkBlocked, match=r"127\.0\.0\.1 is a loopback address"):
            await fetcher.fetch(Request(server.url("/page")))
        with pytest.raises(NetworkBlocked):
            async with fetcher.stream(Request(server.url("/page"))):
                pass

    assert server.received == []


async def test_a_redirect_to_a_loopback_address_is_refused_before_it_is_sent(server: LoopbackServer) -> None:
    port = urlsplit(server.base_url).port
    server.route("/hop", status=302, headers=[("Location", f"http://127.0.0.1:{port}/secret")])
    server.route("/secret", "secret")
    network = NetworkSettings(public_only=True, allow_hosts=("localhost",))

    async with HTTPFetcher(network=network) as fetcher:
        with pytest.raises(NetworkBlocked) as refused:
            await fetcher.fetch(Request(f"http://localhost:{port}/hop"))

    assert str(refused.value) == (
        f"network.public_only refused the redirect from http://localhost:{port}/hop to http://127.0.0.1:{port}/secret: "
        "127.0.0.1 is a loopback address"
    )
    assert [request.path for request in server.received] == ["/hop"]


@pytest.mark.pernix_network  # the host is no loopback name; it is reached at the address it resolves to
async def test_curl_connects_to_the_addresses_checked(
    server: LoopbackServer, resolver: Callable[[Mapping[str, list[str]]], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    server.route("/page", "pinned")
    port = urlsplit(server.base_url).port
    resolver({"pinned.invalid": ["127.0.0.1"]})  # .invalid never resolves, so curl cannot have resolved it itself
    monkeypatch.setattr("pernix.fetchers.address_guard.is_public_address", lambda address: str(address) == "127.0.0.1")

    async with HTTPFetcher(network=GUARDED) as fetcher:
        response = await fetcher.fetch(Request(f"http://pinned.invalid:{port}/page"))

    assert (response.status, response.body, response.ip_address) == (200, b"pinned", "127.0.0.1")
    assert server.received[0].header("Host") == f"pinned.invalid:{port}"


async def test_through_a_proxy_names_are_left_to_the_proxy(
    refused_proxy: str, resolver: Callable[[Mapping[str, list[str]]], None]
) -> None:
    resolver({})

    async with HTTPFetcher(network=GUARDED) as fetcher:
        with pytest.raises(ProxyError):
            await fetcher.fetch(Request("http://unknown.example/", proxy=refused_proxy))


@pytest.mark.pernix_network  # the host is no loopback name, and never resolves
async def test_resolving_a_host_is_limited_by_the_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def slow(*_args: Any, **_kwargs: Any) -> list[Any]:
        time.sleep(0.5)
        return []

    monkeypatch.setattr(socket, "getaddrinfo", slow)

    async with HTTPFetcher(network=GUARDED) as fetcher:
        with pytest.raises(Timeout, match=r"resolving the host of http://slow\.example/ took longer than 0\.1 s"):
            await fetcher.fetch(Request("http://slow.example/", timeout=0.1))


class _Browser:
    """Stands in for the browser Fetcher, recording what it is asked to render."""

    schemes: ClassVar[frozenset[str]] = frozenset()

    def __init__(self) -> None:
        self.rendered: list[Request] = []

    async def fetch(self, request: Request) -> Response:
        self.rendered.append(request)
        return make_response(request.url, "rendered", request=request)


async def test_rendering_is_refused_while_only_public_hosts_may_be_reached() -> None:
    browser = _Browser()
    dispatch = FetcherDispatch({"http": FakeFetcher(), BROWSER_FETCHER: browser}, public_only=True)

    with pytest.raises(NetworkBlocked, match="the browser resolves hosts itself"):
        await dispatch(Request("https://example.com/", render=True))
    with pytest.raises(NetworkBlocked):
        await dispatch(Request("https://example.com/", fetcher=BROWSER_FETCHER))
    assert browser.rendered == []
    assert (await dispatch(Request("https://example.com/"))).status == 404  # the HTTP Fetcher still fetches


async def test_tests_reach_loopback_hosts_unless_they_turn_the_setting_on(
    server: LoopbackServer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server.route("/", "<p>home</p>")
    (tmp_path / "pernix.toml").write_text("[network]\npublic_only = true\n")
    monkeypatch.chdir(tmp_path)

    class Home(Spider):
        start_urls = (server.url("/"),)

        async def parse(self, response: Response) -> AsyncIterator[dict[str, int]]:
            yield {"status": response.status}

    assert (await crawl(Home)).items == [{"status": 200}]
    guarded = await crawl(Home, settings={"network.public_only": True})
    assert (guarded.items, guarded.stats["requests.failed"]) == ([], 1)
    assert len(server.received) == 1
