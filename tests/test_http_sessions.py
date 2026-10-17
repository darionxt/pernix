"""The HTTP Fetcher sending Requests under a Session."""

from __future__ import annotations

import socket
import time
from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from cookie_server import CookieServer
from pernix import Cookie, Request, Session
from pernix.errors import ConnectError, Timeout
from pernix.fetchers import HTTPFetcher, HTTPSettings
from pernix.testing import Reply
from scripted_server import ScriptedServer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from pernix.testing import LoopbackServer, ReceivedRequest


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
def other_server(ca: trustme.CA) -> Iterator[CookieServer]:
    with CookieServer(ca) as server:
        yield server


def _sent(cookies: list[str]) -> list[str]:
    return [cookie for field in cookies for cookie in field.split("; ")]


async def test_a_login_carries_over_to_the_next_request_of_its_session(server: LoopbackServer) -> None:
    def account(request: ReceivedRequest) -> Reply:
        signed_in = "sid=abc" in (request.header("Cookie") or "")
        return Reply(body="welcome" if signed_in else "sign in first", status=200 if signed_in else 403)

    server.route(
        "/login",
        method="POST",
        status=302,
        headers=[("Location", "/home"), ("Set-Cookie", "sid=abc; Path=/; HttpOnly")],
    )
    server.route("/home", "home")
    server.route("/account", handler=account)
    session = Session("me")

    async with HTTPFetcher() as fetcher:
        login = await fetcher.fetch(Request(server.url("/login"), data={"user": "me"}), session=session)
        signed_in = await fetcher.fetch(Request(server.url("/account")), session=session)
        anonymous = await fetcher.fetch(Request(server.url("/account")))

    assert [(cookie.name, cookie.domain, cookie.http_only) for cookie in session.cookies] == [
        ("sid", "127.0.0.1", True)
    ]
    assert (login.session, signed_in.session, anonymous.session) == ("me", "me", None)
    assert (signed_in.status, signed_in.text) == (200, "welcome")
    assert anonymous.status == 403
    assert dict(signed_in.cookies) == {}  # the Session's cookies stay in the Session


async def test_two_sessions_never_share_a_connection_or_a_cookie(cookie_server: CookieServer) -> None:
    cookie_server.routes["/sign-a"] = [("Set-Cookie", "a=1")]
    cookie_server.routes["/sign-b"] = [("Set-Cookie", "b=2")]
    first, second = Session("first"), Session("second")

    async with HTTPFetcher() as fetcher:
        for request, session in [
            (Request(cookie_server.url("/sign-a", scheme="https")), first),
            (Request(cookie_server.url("/sign-b", scheme="https")), second),
            (Request(cookie_server.url("/check", scheme="https")), first),
            (Request(cookie_server.url("/check", scheme="https")), second),
            (Request(cookie_server.url("/check", scheme="https")), None),
        ]:
            await fetcher.fetch(request, session=session)

    sign_a, sign_b, check_a, check_b, check_none = cookie_server.received
    assert {hop.protocol for hop in cookie_server.received} == {"HTTP/2"}  # connections that stay open
    assert sign_a.peer == check_a.peer
    assert sign_b.peer == check_b.peer
    assert len({sign_a.peer, sign_b.peer, check_none.peer}) == 3
    assert (check_a.cookies, check_b.cookies, check_none.cookies) == (["a=1"], ["b=2"], [])
    assert ([cookie.name for cookie in first.cookies], [cookie.name for cookie in second.cookies]) == (["a"], ["b"])


async def test_each_redirect_hop_gets_the_session_cookies_matching_it(cookie_server: CookieServer) -> None:
    cookie_server.routes["/start"] = [("Location", "http://b.example.test/end")]
    session = Session(
        cookies=[
            Cookie(name="everywhere", value="1", domain="example.test", host_only=False),
            Cookie(name="here", value="1", domain="a.example.test"),
            Cookie(name="end_only", value="1", domain="example.test", host_only=False, path="/end"),
            Cookie(name="secure", value="1", domain="example.test", host_only=False, secure=True),
            Cookie(name="expired", value="1", domain="example.test", host_only=False, expires=1.0),
        ]
    )

    async with HTTPFetcher() as fetcher:  # through the server as a proxy, for any host name
        await fetcher.fetch(Request("http://a.example.test/start", proxy=cookie_server.proxy), session=session)

    assert [(hop.host, sorted(_sent(hop.cookies))) for hop in cookie_server.received] == [
        ("a.example.test", ["everywhere=1", "here=1"]),
        ("b.example.test", ["end_only=1", "everywhere=1"]),
    ]  # sorted, as curl orders them by domain length too, unlike a browser


async def test_cookies_set_on_every_hop_are_kept_in_the_session(cookie_server: CookieServer) -> None:
    cookie_server.routes["/start"] = [
        ("Location", "http://www.example.test/end"),
        ("Set-Cookie", "shared=1; Domain=example.test"),
        ("Set-Cookie", "start=1; Path=/"),
    ]
    cookie_server.routes["/end"] = [("Set-Cookie", "end=1; SameSite=Lax"), ("Set-Cookie", "start=; Domain=a.test")]
    session = Session()

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(
            Request("http://a.example.test/start", proxy=cookie_server.proxy), session=session
        )

    assert sorted((cookie.name, cookie.domain, cookie.host_only, cookie.same_site) for cookie in session.cookies) == [
        ("end", "www.example.test", True, "lax"),
        ("shared", "example.test", False, None),
        ("start", "a.example.test", True, None),
    ]  # the cookie for another domain was refused
    assert dict(response.cookies) == {"shared": "1", "start": "", "end": "1"}


async def test_the_requests_own_cookies_win_and_are_not_kept(cookie_server: CookieServer) -> None:
    session = Session(
        cookies=[
            Cookie(name="lang", value="fr", domain="127.0.0.1"),
            Cookie(name="kept", value="1", domain="127.0.0.1"),
        ]
    )

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(cookie_server.url("/"), cookies={"lang": "en", "extra": "1"}), session=session)

    [received] = cookie_server.received
    assert sorted(_sent(received.cookies)) == ["extra=1", "kept=1", "lang=en"]
    assert [(cookie.name, cookie.value) for cookie in session.cookies] == [("lang", "fr"), ("kept", "1")]


async def test_the_session_proxy_applies_unless_the_request_names_its_own(
    cookie_server: CookieServer, other_server: CookieServer
) -> None:
    session = Session(proxy=cookie_server.proxy)

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request("http://intranet/via-session"), session=session)
        await fetcher.fetch(Request("http://intranet/via-request", proxy=other_server.proxy), session=session)

    assert [hop.path for hop in cookie_server.received] == ["/via-session"]
    assert [hop.path for hop in other_server.received] == ["/via-request"]


async def test_the_session_profile_applies_unless_the_request_names_its_own(cookie_server: CookieServer) -> None:
    session = Session(profile="firefox147")

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(cookie_server.url("/session")), session=session)
        await fetcher.fetch(Request(cookie_server.url("/request"), impersonate="chrome150"), session=session)

    by_session, by_request = cookie_server.received
    assert "Firefox/147" in by_session.user_agent
    assert "Chrome/150" in by_request.user_agent
    assert by_session.peer != by_request.peer


async def test_a_session_profile_must_be_pinned(cookie_server: CookieServer) -> None:
    async with HTTPFetcher() as fetcher:
        with pytest.raises(ValueError, match="Profile"):
            await fetcher.fetch(Request(cookie_server.url("/")), session=Session(profile="chrome"))


async def test_a_request_naming_another_session_is_refused(cookie_server: CookieServer) -> None:
    async with HTTPFetcher() as fetcher:
        with pytest.raises(ValueError, match="'other'"):
            await fetcher.fetch(Request(cookie_server.url("/"), session="other"), session=Session("mine"))
        response = await fetcher.fetch(Request(cookie_server.url("/"), session="mine"), session=Session("mine"))

    assert response.session == "mine"


async def test_a_streamed_fetch_keeps_its_cookies_in_the_session(cookie_server: CookieServer) -> None:
    cookie_server.routes["/export"] = [("Set-Cookie", "seen=1")]
    session = Session(cookies=[Cookie(name="sid", value="abc", domain="127.0.0.1")])

    async with (
        HTTPFetcher() as fetcher,
        fetcher.stream(Request(cookie_server.url("/export")), session=session) as streamed,
    ):
        assert streamed.response.session == session.id

    assert cookie_server.received[0].cookies == ["sid=abc"]
    assert sorted(cookie.name for cookie in session.cookies) == ["seen", "sid"]


async def test_cookies_are_kept_once_the_headers_arrived() -> None:
    head = b"HTTP/1.1 200 OK\r\nSet-Cookie: seen=1\r\nContent-Length: 10\r\n\r\n"
    session = Session()

    with ScriptedServer([(head + b"start", 0), (b"late!", 2)]) as server:
        async with HTTPFetcher(HTTPSettings(deadline=0.5)) as fetcher:
            with pytest.raises(Timeout):
                await fetcher.fetch(Request(server.url), session=session)

    assert [cookie.name for cookie in session.cookies] == ["seen"]


async def test_session_cookies_a_cookie_line_cannot_carry_are_left_out(cookie_server: CookieServer) -> None:
    session = Session(
        cookies=[
            Cookie(name="tab", value="a\tb", domain="127.0.0.1"),
            Cookie(name="line", value="a\nb", domain="127.0.0.1"),
            Cookie(name="ok", value="1", domain="127.0.0.1"),
        ]
    )

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(cookie_server.url("/")), session=session)

    assert cookie_server.received[0].cookies == ["ok=1"]


async def test_cookies_set_before_a_failing_hop_are_kept(server: LoopbackServer) -> None:
    with socket.create_server(("127.0.0.1", 0)) as closed:
        port = closed.getsockname()[1]
    server.route("/login", status=302, headers=[("Location", f"http://127.0.0.1:{port}/"), ("Set-Cookie", "sid=abc")])
    session = Session()

    async with HTTPFetcher() as fetcher:
        with pytest.raises(ConnectError):
            await fetcher.fetch(Request(server.url("/login")), session=session)

    assert [cookie.name for cookie in session.cookies] == ["sid"]


async def test_a_redirect_cookie_expires_from_when_its_hop_arrived(server: LoopbackServer) -> None:
    def slow(request: ReceivedRequest) -> Reply:
        time.sleep(1.5)
        return Reply(body="done")

    server.route("/start", status=302, headers=[("Location", "/slow"), ("Set-Cookie", "brief=1; Max-Age=1")])
    server.route("/slow", handler=slow)
    session = Session()
    started = time.time()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/start")), session=session)

    [brief] = session.cookies
    assert brief.expires is not None
    assert brief.expires < started + 1.4  # not 1 s after the last hop
    assert session.cookies.matching(server.url("/")) == []


async def test_a_proxys_answer_to_connect_sets_no_cookies(cookie_server: CookieServer) -> None:
    cookie_server.connect_headers = [("Set-Cookie", "proxy=1")]
    cookie_server.routes["/"] = [("Set-Cookie", "origin=1")]
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request("https://intranet/", proxy=cookie_server.proxy), session=session)

    assert [(cookie.name, cookie.domain) for cookie in session.cookies] == [("origin", "intranet")]


async def test_informational_responses_set_no_cookies(cookie_server: CookieServer) -> None:
    cookie_server.early_hints["/"] = [("Link", "</style.css>; rel=preload"), ("Set-Cookie", "early=1")]
    cookie_server.routes["/"] = [("Set-Cookie", "final=1")]
    session = Session()

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(cookie_server.url("/")), session=session)

    assert response.status == 200
    assert [cookie.name for cookie in session.cookies] == ["final"]
