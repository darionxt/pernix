"""How the HTTP Fetcher sends cookies, to every kind of host and redirect hop, over HTTP/1.1 and HTTP/2."""

from __future__ import annotations

from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme
from curl_cffi import CurlECode

from cookie_server import CookieServer
from pernix import Request
from pernix.errors import FetchError
from pernix.fetchers import HTTPFetcher

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


@PROTOCOLS
async def test_request_cookies_keep_their_order_whatever_the_length_of_their_names(
    server: CookieServer, scheme: str, protocol: str
) -> None:
    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(_request(server, "localhost", "/", scheme, {"a": "1", "long": "2", "mid": "3"}))

    [received] = server.received
    assert (received.protocol, _sent(received.cookies)) == (protocol, ["a=1", "long=2", "mid=3"])


@pytest.mark.parametrize(
    ("scheme", "host", "expected"),
    [
        ("https", DOTLESS, ["plain=3", "__Host-b=2", "__Secure-a=1"]),
        ("http", DOTLESS, ["plain=3"]),
        ("http", "localhost", ["plain=3", "__Host-b=2", "__Secure-a=1"]),  # loopback HTTP counts as secure
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


@pytest.mark.parametrize(
    ("host", "ascii_host"),
    [
        ("STRAẞE.de", "xn--strae-oqa.de"),  # strasse.de with curl's conversion on Linux
        ("👍ß.example", "xn--zca1286w.example"),  # xn--ss-cm72a.example on Linux
        ("♥ς.example", "xn--3xa748o.example"),  # xn--4xa548o.example on Linux
    ],
)
async def test_international_hosts_are_reached_as_browsers_reach_them(
    server: CookieServer, host: str, ascii_host: str
) -> None:
    async with HTTPFetcher() as fetcher:  # through the server as a proxy, which records the host it is asked for
        response = await fetcher.fetch(Request(f"http://{host}/page", cookies={"a": "1"}, proxy=server.proxy))

    assert [(hop.host, hop.cookies) for hop in server.received] == [(ascii_host, ["a=1"])]
    assert response.url == f"http://{ascii_host}/page"


@pytest.mark.parametrize("host", ["a\u200db.example", "aא.example", "a%20b.example"])
async def test_a_host_whatwg_urls_reject_fails_the_fetch_before_connecting(server: CookieServer, host: str) -> None:
    request = Request(f"http://{host}/", cookies={"a": "1"}, proxy=server.proxy)  # ab.example, ... on Linux

    async with HTTPFetcher() as fetcher:
        with pytest.raises(FetchError, match="WHATWG URLs reject it") as fetched:
            await fetcher.fetch(request)
        with pytest.raises(FetchError, match="WHATWG URLs reject it") as streamed:
            async with fetcher.stream(request):
                pass

    assert [(type(error.value), error.value.code) for error in (fetched, streamed)] == [
        (FetchError, CurlECode.URL_MALFORMAT)
    ] * 2  # not retried
    assert server.received == []


@PROTOCOLS
async def test_cookies_set_on_the_way_join_those_of_the_request_in_browser_order(
    server: CookieServer, scheme: str, protocol: str
) -> None:
    server.routes["/start"] = [
        ("Location", "/end"),
        ("Set-Cookie", "deep=4; Path=/end"),
        ("Set-Cookie", "a=new"),
        ("Set-Cookie", "z=5; Path=/"),
    ]
    cookies = {"session": "1", "a": "2", "lang": "3"}  # curl alone would send the longest names first

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(_request(server, "localhost", "/start", scheme, cookies))

    start, end = server.received
    assert (start.protocol, start.cookies) == (
        protocol,
        ["session=1; a=2; lang=3"] if protocol == "HTTP/1.1" else ["session=1", "a=2", "lang=3"],
    )
    assert (end.protocol, _sent(end.cookies)) == (
        protocol,
        ["deep=4", "session=1", "a=new", "lang=3", "z=5"],  # the longer path first; the replaced cookie in its place
    )
    assert dict(response.cookies) == {"session": "1", "a": "new", "lang": "3", "deep": "4", "z": "5"}


@PROTOCOLS
async def test_a_cookie_header_is_sent_as_the_requests_cookies(
    server: CookieServer, other_server: CookieServer, scheme: str, protocol: str
) -> None:
    other_scheme = "https" if scheme == "http" else "http"
    server.routes["/start"] = [
        ("Location", server.url("/again", host="localhost", scheme=other_scheme)),
        ("Set-Cookie", "set=3"),
    ]
    server.routes["/again"] = [("Location", other_server.url("/elsewhere", host="localhost", scheme=scheme))]
    other_server.routes["/elsewhere"] = [("Location", server.url("/away", host="127.0.0.1"))]
    request = Request(server.url("/start", host="localhost", scheme=scheme), headers={"Cookie": "b=2; a=1"})

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(request)

    hops = [*server.received[:2], *other_server.received, *server.received[2:]]
    assert [(hop.path, _sent(hop.cookies)) for hop in hops] == [
        ("/start", ["b=2", "a=1"]),
        ("/again", ["b=2", "a=1", "set=3"]),  # another scheme
        ("/elsewhere", ["b=2", "a=1", "set=3"]),  # another port
        ("/away", []),  # another host
    ]
    assert hops[0].protocol == protocol
    assert all(len(hop.cookies) <= 1 for hop in hops if hop.protocol == "HTTP/1.1")  # one Cookie header only
    assert dict(response.cookies) == {"b": "2", "a": "1", "set": "3"}
