"""How the HTTP Fetcher sends `Request.cookies`, to every kind of host, over HTTP/1.1 and HTTP/2."""

from __future__ import annotations

from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from cookie_server import CookieServer
from pernix import Request
from pernix.fetchers import HTTPFetcher
from pernix.fetchers.http_fetcher import _cookie_host  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

DOTLESS = "intranet"
"""A host name without a dot, reached through the server as a proxy."""

PROTOCOLS = pytest.mark.parametrize(
    ("scheme", "protocol"), [("http", "HTTP/1.1"), ("https", "HTTP/2")], ids=["HTTP/1.1", "HTTP/2"]
)


@pytest.fixture
def ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> trustme.CA:
    """A certificate authority that curl trusts, and that the servers' certificates come from."""
    ca = trustme.CA()
    bundle = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(bundle))
    monkeypatch.setattr(curl_cffi.aio, "DEFAULT_CACERT", str(bundle))  # read by each new async curl session
    return ca


@pytest.fixture
def server(ca: trustme.CA) -> Iterator[CookieServer]:
    with CookieServer(ca) as server:
        yield server


@pytest.fixture
def other_server(ca: trustme.CA) -> Iterator[CookieServer]:
    with CookieServer(ca) as server:
        yield server


@pytest.fixture
def ipv6_server(ca: trustme.CA) -> Iterator[CookieServer]:
    try:
        server = CookieServer(ca, "::1")
    except OSError:
        pytest.skip("the IPv6 loopback address is not available")
    with server:
        yield server


def _request(server: CookieServer, host: str, path: str, scheme: str, cookies: dict[str, str]) -> Request:
    if host == DOTLESS:
        return Request(f"{scheme}://{host}{path}", cookies=cookies, proxy=server.proxy)
    return Request(server.url(path, host=host, scheme=scheme), cookies=cookies)


def _sent(cookies: list[str]) -> list[str]:
    """The cookies of a request, one by one, whether in one header or in many fields."""
    return [cookie for field in cookies for cookie in field.split("; ")]


@PROTOCOLS
@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "app.localhost", DOTLESS, "::1"])
async def test_request_cookies_reach_any_host_in_the_order_given(
    host: str, scheme: str, protocol: str, request: pytest.FixtureRequest
) -> None:
    server: CookieServer = request.getfixturevalue("ipv6_server" if host == "::1" else "server")

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, host, "/", scheme, {"c": "3", "a": "1", "b": "2"}))

    [received] = server.received
    expected = ["c=3; a=1; b=2"] if protocol == "HTTP/1.1" else ["c=3", "a=1", "b=2"]  # one field each, as Chrome
    assert (received.protocol, received.cookies) == (protocol, expected)


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("http://Example.COM/a", "example.com"),
        ("http://[::1]:8080/", "::1"),
        ("https://bücher.example/", "xn--bcher-kva.example"),
        ("https://faß.example/", "xn--fa-hia.example"),  # not fass.example
        ("https://Faß.Example:8443/", "xn--fa-hia.example"),
        ("http://other.example\\@faß.example/", "xn--fa-hia.example"),  # the host curl connects to
        ("http://faß%2Eexample/", "xn--fa-hia.example"),  # percent-decoded first, as curl does
        ("http://fa%C3%9F.example/", "xn--fa-hia.example"),
        ("http://Local%48ost/", "localhost"),
        ("https://ΣΑΣ/", "xn--mxa9ab"),  # not the final sigma str.lower() gives, xn--mxa8ab
        ("https://a\u200db.example/", None),  # curl gives xn--ab-m1t on macOS, ab on Linux
        ("http://localhost./", "localhost."),  # curl keeps the trailing dot
        ("http:///path", None),
    ],
)
def test_cookie_hosts_are_named_as_curl_matches_them(url: str, host: str | None) -> None:
    assert _cookie_host(url) == host


@PROTOCOLS
async def test_longer_cookie_names_go_first(server: CookieServer, scheme: str, protocol: str) -> None:
    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, "localhost", "/", scheme, {"a": "1", "long": "2", "mid": "3"}))

    [received] = server.received
    assert (received.protocol, _sent(received.cookies)) == (protocol, ["long=2", "mid=3", "a=1"])


@pytest.mark.parametrize(
    ("scheme", "host", "expected"),
    [
        ("https", DOTLESS, ["__Secure-a=1", "__Host-b=2", "plain=3"]),
        ("http", DOTLESS, ["plain=3"]),
        ("http", "localhost", ["__Secure-a=1", "__Host-b=2", "plain=3"]),  # loopback HTTP counts as secure
    ],
)
async def test_secure_only_prefixes_go_over_https_only(
    server: CookieServer, scheme: str, host: str, expected: list[str]
) -> None:
    cookies = {"plain": "3", "__Host-b": "2", "__Secure-a": "1"}

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, host, "/", scheme, cookies))

    [received] = server.received
    assert _sent(received.cookies) == expected


async def test_request_cookies_follow_redirects_on_their_host(server: CookieServer, other_server: CookieServer) -> None:
    server.routes["/start"] = [("Location", server.url("/upgraded", host="localhost", scheme="https"))]
    server.routes["/upgraded"] = [("Location", other_server.url("/moved", host="localhost"))]

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(_request(server, "localhost", "/start", "http", {"a": "1", "b": "2"}))

    assert response.url == other_server.url("/moved", host="localhost")
    hops = [*server.received, *other_server.received]
    assert [(hop.path, _sent(hop.cookies)) for hop in hops] == [
        ("/start", ["a=1", "b=2"]),
        ("/upgraded", ["a=1", "b=2"]),  # another scheme
        ("/moved", ["a=1", "b=2"]),  # another port
    ]


@PROTOCOLS
async def test_request_cookies_never_follow_a_redirect_to_another_host(
    server: CookieServer, scheme: str, protocol: str
) -> None:
    server.routes["/start"] = [("Location", server.url("/end", host="localhost", scheme=scheme))]

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, "127.0.0.1", "/start", scheme, {"a": "1", "b": "2"}))

    assert [(hop.protocol, hop.host.split(":")[0], _sent(hop.cookies)) for hop in server.received] == [
        (protocol, "127.0.0.1", ["a=1", "b=2"]),
        (protocol, "localhost", []),
    ]


async def test_request_cookies_stay_on_an_international_host(server: CookieServer) -> None:
    server.routes["/start"] = [("Location", server.url("/end", host="fass.localhost"))]

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, "faß.localhost", "/start", "http", {"a": "1"}))

    assert [(hop.host.split(":")[0], hop.cookies) for hop in server.received] == [
        ("xn--fa-hia.localhost", ["a=1"]),
        ("fass.localhost", []),  # another host
    ]


async def test_request_cookies_reach_a_percent_encoded_host(server: CookieServer) -> None:
    async with HTTPFetcher() as fetcher:  # through a proxy, as the network guard reads the host undecoded
        await fetcher.fetch(Request("http://faß%2Elocalhost/", cookies={"a": "1"}, proxy=server.proxy))

    assert [(hop.host, hop.cookies) for hop in server.received] == [("xn--fa-hia.localhost", ["a=1"])]


async def test_request_cookies_stay_on_the_host_curl_connects_to(server: CookieServer) -> None:
    server.routes["/start"] = [("Location", server.url("/end", host="other.localhost"))]

    async with HTTPFetcher() as fetcher:  # WHATWG URLs would take other.localhost as the host
        await fetcher.fetch(_request(server, "other.localhost\\@faß.localhost", "/start", "http", {"a": "1"}))

    assert [(hop.host.split(":")[0], hop.cookies) for hop in server.received] == [
        ("xn--fa-hia.localhost", ["a=1"]),
        ("other.localhost", []),
    ]


@PROTOCOLS
async def test_cookies_set_on_the_way_join_those_of_the_request(
    server: CookieServer, scheme: str, protocol: str
) -> None:
    server.routes["/start"] = [("Location", "/end"), ("Set-Cookie", "a=new; Path=/"), ("Set-Cookie", "fresh=1")]

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(_request(server, DOTLESS, "/start", scheme, {"a": "1", "b": "2"}))

    start, end = server.received
    assert (start.protocol, _sent(start.cookies)) == (protocol, ["a=1", "b=2"])
    assert sorted(_sent(end.cookies)) == ["a=new", "b=2", "fresh=1"]
    assert dict(response.cookies) == {"a": "new", "b": "2", "fresh": "1"}
