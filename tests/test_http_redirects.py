"""How the HTTP Fetcher follows redirects, one hop at a time, as browsers do."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import curl_cffi.aio
import pytest
import trustme
from curl_cffi import CurlECode

from cookie_server import CookieServer
from pernix import IgnoreRequest, Request
from pernix.errors import FetchError, NetworkBlocked, TooManyRedirects
from pernix.fetchers import HTTPFetcher, HTTPSettings
from pernix.fetchers.http_fetcher import _checked_redirect  # pyright: ignore[reportPrivateUsage]
from pernix.fetchers.network_guard import blocking_network
from pernix.fetchers.redirects import checking_redirects, following_at_most
from pernix.testing import LoopbackServer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pernix.testing import ReceivedRequest

METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE")

BODY_HEADERS = {"Content-Type": "text/plain", "Content-Language": "en", "Content-Location": "/form"}


@pytest.fixture
def ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> trustme.CA:
    ca = trustme.CA()
    bundle = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(bundle))
    monkeypatch.setattr(curl_cffi.aio, "DEFAULT_CACERT", str(bundle))  # read by each new async curl session
    return ca


@pytest.fixture
def cookie_server(ca: trustme.CA) -> Iterator[CookieServer]:
    with CookieServer(ca) as server:
        yield server


@pytest.fixture
def other_server() -> Iterator[LoopbackServer]:
    with LoopbackServer() as server:
        yield server


def _redirect(server: LoopbackServer, path: str, status: int, location: str) -> None:
    for method in METHODS:
        server.route(path, "moved", status=status, headers=[("Location", location)], method=method)


def _answer(server: LoopbackServer, path: str, body: str = "end") -> None:
    for method in METHODS:
        server.route(path, body, method=method)


@pytest.mark.parametrize(
    ("status", "method", "body", "followed_with"),
    [
        (301, "POST", b"a=1", "GET"),
        (302, "POST", b"a=1", "GET"),
        (303, "POST", b"a=1", "GET"),
        (303, "PUT", b"a=1", "GET"),
        (303, "DELETE", None, "GET"),
        (303, "HEAD", None, "HEAD"),
        (303, "GET", None, "GET"),
        (301, "PUT", b"a=1", "PUT"),  # only a POST turns into a GET
        (302, "DELETE", None, "DELETE"),
        (302, "GET", None, "GET"),
        (307, "POST", b"a=1", "POST"),
        (307, "POST", None, "POST"),
        (308, "PUT", b"a=1", "PUT"),
        (308, "HEAD", None, "HEAD"),
    ],
)
async def test_redirects_change_the_method_and_body_as_browsers_do(
    server: LoopbackServer, status: int, method: str, body: bytes | None, followed_with: str
) -> None:
    _redirect(server, "/start", status, "/end")
    _answer(server, "/end")
    headers = {**(BODY_HEADERS if body is not None else {}), "X-Kept": "1"}

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/start"), method=method, body=body, headers=headers))

    start, end = server.received
    assert (start.method, start.body) == (method, body or b"")
    assert (end.method, end.header("X-Kept")) == (followed_with, "1")
    kept = followed_with == method
    assert end.body == (body if kept and body is not None else b"")
    assert [end.header(name) for name in BODY_HEADERS] == [
        start.header(name) if kept else None for name in BODY_HEADERS
    ]
    assert [(hop.url, hop.status) for hop in response.history] == [(server.url("/start"), status)]
    assert (response.url, response.status) == (server.url("/end"), 200)


async def test_credentials_never_follow_a_redirect_to_another_origin(
    server: LoopbackServer, other_server: LoopbackServer
) -> None:
    _redirect(server, "/start", 307, "/same")
    _redirect(server, "/same", 302, other_server.url("/port"))
    _redirect(other_server, "/port", 301, server.url("/back"))
    _answer(server, "/back")
    _redirect(server, "/host", 308, server.url("/back").replace("127.0.0.1", "localhost"))
    credentials = {"Authorization": "Basic dXNlcjpwYXNz", "Proxy-Authorization": "Basic cHJveHk6cGFzcw=="}
    headers = {**credentials, "X-Kept": "1"}

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/start"), method="POST", body=b"a=1", headers=headers))
        await fetcher.fetch(Request(server.url("/host"), headers=headers))

    def sent(request: ReceivedRequest) -> tuple[str, list[str | None]]:
        return request.path, [request.header(name) for name in headers]

    start, same, back, host, host_back = server.received
    [port] = other_server.received
    assert [sent(hop) for hop in (start, same, port, back)] == [
        ("/start", [*credentials.values(), "1"]),
        ("/same", [*credentials.values(), "1"]),  # the same origin
        ("/port", [None, None, "1"]),  # another port
        ("/back", [None, None, "1"]),  # back on the first origin, but dropped on the way
    ]
    assert [sent(hop) for hop in (host, host_back)] == [
        ("/host", [*credentials.values(), "1"]),
        ("/back", [None, None, "1"]),  # another host
    ]


async def test_each_redirect_is_recorded_in_the_history(server: LoopbackServer) -> None:
    for status, path, location in [
        (301, "/a", "/b"),
        (302, "/b", "c"),
        (303, "/c", server.url("/d")),
        (307, "/d", f"//127.0.0.1:{urlsplit(server.base_url).port}/e"),
        (308, "/e", "/end?q=1"),
    ]:
        server.route(path, status=status, headers=[("Location", location), ("X-Hop", path)])
    server.route("/end", "done")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/a")))

    assert [(hop.url, hop.status, hop.headers.get("X-Hop")) for hop in response.history] == [
        (server.url("/a"), 301, "/a"),
        (server.url("/b"), 302, "/b"),
        (server.url("/c"), 303, "/c"),
        (server.url("/d"), 307, "/d"),
        (server.url("/e"), 308, "/e"),
    ]
    assert (response.url, response.status, response.text) == (server.url("/end?q=1"), 200, "done")
    assert [hop.path for hop in server.received] == ["/a", "/b", "/c", "/d", "/e", "/end"]


@pytest.mark.parametrize(("status", "location"), [(300, "/end"), (304, "/end"), (305, "/end"), (302, None)])
async def test_responses_browsers_do_not_follow_are_final(
    server: LoopbackServer, status: int, location: str | None
) -> None:
    server.route("/start", status=status, headers=[] if location is None else [("Location", location)])
    server.route("/end", "end")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/start")))

    assert (response.status, response.history, [hop.path for hop in server.received]) == (status, (), ["/start"])


async def test_an_empty_location_is_skipped_for_the_next(server: LoopbackServer) -> None:
    server.route("/start", status=302, headers=[("Location", ""), ("Location", "/end")])
    server.route("/end", "end")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/start")))

    assert (response.url, response.text) == (server.url("/end"), "end")


async def test_a_location_beyond_ascii_is_percent_encoded(server: LoopbackServer) -> None:
    server.route("/start", status=302, headers=[("Location", "/café")])  # sent as Latin-1, a byte over 0x7f
    server.route("/caf%E9", "café")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/start")))

    assert (response.url, response.text) == (server.url("/caf%E9"), "café")


@pytest.mark.parametrize("max_redirects", [0, 2])
async def test_more_redirects_than_allowed_fail_before_the_next_is_sent(
    server: LoopbackServer, max_redirects: int
) -> None:
    for n in range(4):
        server.route(f"/r{n}", status=302, headers=[("Location", f"/r{n + 1}")])

    async with HTTPFetcher(HTTPSettings(max_redirects=max_redirects)) as fetcher:
        with pytest.raises(TooManyRedirects, match=rf"redirected more than http\.max_redirects \({max_redirects}\)"):
            await fetcher.fetch(Request(server.url("/r0")))

    assert [hop.path for hop in server.received] == [f"/r{n}" for n in range(max_redirects + 1)]


@pytest.mark.parametrize(("lower", "followed"), [(2, 2), (5, 3)])
async def test_a_fetch_follows_fewer_redirects_when_asked_to_but_never_more(
    server: LoopbackServer, lower: int, followed: int
) -> None:
    for n in range(5):
        server.route(f"/r{n}", status=302, headers=[("Location", f"/r{n + 1}")])

    async with HTTPFetcher(HTTPSettings(max_redirects=3)) as fetcher:
        with following_at_most(lower), pytest.raises(TooManyRedirects) as raised:
            await fetcher.fetch(Request(server.url("/r0")))
        response = await fetcher.fetch(Request(server.url("/r2")))  # outside the block, three are followed

    limit = f"{followed}" if lower < 3 else r"http\.max_redirects \(3\)"
    assert raised.match(rf"redirected more than {limit} times")
    assert [hop.path for hop in server.received][: followed + 1] == [f"/r{n}" for n in range(followed + 1)]
    assert response.url == server.url("/r5")


@pytest.mark.parametrize(
    ("location", "code", "problem"),
    [
        ("http://[unclosed/page", CurlECode.URL_MALFORMAT, "not a valid URL"),
        ("http://exa mple.test/", CurlECode.URL_MALFORMAT, "not a valid URL"),
        ("ftp://127.0.0.1/file", CurlECode.UNSUPPORTED_PROTOCOL, "neither an http nor an https URL"),
        ("data:text/plain,hello", CurlECode.UNSUPPORTED_PROTOCOL, "neither an http nor an https URL"),
    ],
)
async def test_a_redirect_browsers_refuse_to_follow_fails_the_fetch(
    server: LoopbackServer, location: str, code: int, problem: str
) -> None:
    server.route("/start", status=302, headers=[("Location", location)])

    async with HTTPFetcher() as fetcher:
        with blocking_network() as guard, pytest.raises(FetchError, match=problem) as refused:
            await fetcher.fetch(Request(server.url("/start")))

    assert (type(refused.value), refused.value.code) == (FetchError, code)  # not retried
    assert ([hop.path for hop in server.received], guard.blocked) == (["/start"], [])


@pytest.mark.parametrize(
    ("first", "then", "protocols"),
    [("http", "https", ["HTTP/1.1", "HTTP/2"]), ("https", "http", ["HTTP/2", "HTTP/1.1"])],
    ids=["upgraded", "downgraded"],
)
async def test_each_hop_uses_the_http_version_of_its_scheme(
    cookie_server: CookieServer, first: str, then: str, protocols: list[str]
) -> None:
    cookie_server.routes["/start"] = [("Location", cookie_server.url("/end", host="localhost", scheme=then))]

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(cookie_server.url("/start", host="localhost", scheme=first)))

    assert [hop.protocol for hop in cookie_server.received] == protocols
    assert response.http_version == protocols[-1]


async def test_a_loopback_proxy_takes_every_hop(server: LoopbackServer) -> None:
    server.route("/start", status=302, headers=[("Location", "http://shop.example/end")])
    server.route("/end", "proxied")

    async with HTTPFetcher() as fetcher:
        with blocking_network() as guard:
            response = await fetcher.fetch(Request("http://pernix.test/start", proxy=server.base_url))

    assert (response.url, response.text) == ("http://shop.example/end", "proxied")
    assert [hop.header("Host") for hop in server.received] == ["pernix.test", "shop.example"]
    assert guard.blocked == []


async def test_only_the_last_body_is_streamed_and_limited(server: LoopbackServer) -> None:
    server.route("/start", "m" * 5000, status=302, headers=[("Location", "/end")])
    server.route("/end", "streamed")

    async with HTTPFetcher(HTTPSettings(max_body_size=100)) as fetcher:
        async with fetcher.stream(Request(server.url("/start")), max_bytes=10) as streamed:
            body = b"".join([chunk async for chunk in streamed.iter_bytes()])
        fetched = await fetcher.fetch(Request(server.url("/start")))

    assert (streamed.response.url, body) == (server.url("/end"), b"streamed")
    assert [(hop.url, hop.status) for hop in streamed.response.history] == [(server.url("/start"), 302)]
    assert fetched.body == b"streamed"


class Checks:
    """Records the redirects it checks, refusing those that lead to a path."""

    def __init__(self, refused: str | None = None) -> None:
        self.refused = refused
        self.urls: list[str] = []

    async def __call__(self, url: str) -> None:
        self.urls.append(url)
        if self.refused is not None and urlsplit(url).path == self.refused:
            raise IgnoreRequest("refused")


async def test_inside_checking_redirects_each_redirect_is_checked_before_it_is_followed(
    server: LoopbackServer, other_server: LoopbackServer
) -> None:
    _redirect(server, "/a", 302, "/b")
    _redirect(server, "/b", 307, other_server.url("/c"))
    _answer(other_server, "/c")
    checks = Checks()

    async with HTTPFetcher() as fetcher:
        with checking_redirects(checks):
            response = await fetcher.fetch(Request(server.url("/a")))
            async with fetcher.stream(Request(server.url("/b"))) as streamed:
                pass
        await fetcher.fetch(Request(server.url("/a")))  # outside the block, none is checked

    assert (response.url, streamed.response.url) == (other_server.url("/c"), other_server.url("/c"))
    assert checks.urls == [server.url("/b"), other_server.url("/c"), other_server.url("/c")]


async def test_a_redirect_the_check_refuses_is_not_followed(server: LoopbackServer) -> None:
    _redirect(server, "/a", 302, "/b")
    _redirect(server, "/b", 302, "/c")
    _answer(server, "/c")

    async with HTTPFetcher() as fetcher:
        with checking_redirects(Checks(refused="/c")), pytest.raises(IgnoreRequest):
            await fetcher.fetch(Request(server.url("/a")))

    assert [hop.path for hop in server.received] == ["/a", "/b"]


async def test_a_redirect_the_network_guard_refuses_is_never_checked(server: LoopbackServer) -> None:
    server.route("/a", status=302, headers=[("Location", "http://shop.example/b")])
    checks = Checks()

    async with HTTPFetcher() as fetcher:
        with blocking_network(), checking_redirects(checks), pytest.raises(NetworkBlocked):
            await fetcher.fetch(Request(server.url("/a")))

    assert checks.urls == []


@pytest.mark.parametrize("refused", [None, "/b"])
async def test_the_deadline_is_lifted_while_a_redirect_is_checked_and_moved_on_after(refused: str | None) -> None:
    async with asyncio.timeout(60) as deadline:
        lifted: list[float | None] = []
        checks = Checks(refused)

        async def check(url: str) -> None:
            lifted.append(deadline.when())
            await checks(url)

        when = deadline.when()
        assert when is not None
        with checking_redirects(check), pytest.raises(IgnoreRequest) if refused else contextlib.nullcontext():
            await _checked_redirect("http://127.0.0.1/b", deadline)
        moved = deadline.when()

    assert lifted == [None]
    assert moved is not None
    assert moved >= when
