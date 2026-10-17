"""The browser Fetcher under Sessions, against the loopback server: contexts kept per Session, and their state."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import pytest

from pernix import Cookie, Request, Session
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.network_guard import blocking_network
from pernix.testing import ReceivedRequest, Reply

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]

REMEMBER = """<html><body><p id="seen"></p><script>
document.getElementById("seen").textContent = localStorage.getItem("visit") || "first";
localStorage.setItem("visit", "again");
</script></body></html>"""


def _render(url: str, **kwargs: object) -> Request:
    return Request(url, render=True, **kwargs)  # pyright: ignore[reportArgumentType]


def _cookie_echo(request: ReceivedRequest) -> Reply:
    return Reply(f"<p>{request.header('Cookie') or ''}</p>", headers=HTML)


def _on(server: LoopbackServer, host: str, path: str) -> str:
    """A URL of the server under another loopback host name, such as one under `.localhost`."""
    return server.url(path).replace("127.0.0.1", host, 1)


async def test_a_page_under_a_session_sends_its_cookies_and_keeps_those_it_sets(server: LoopbackServer) -> None:
    server.route(
        "/login",
        "<script>document.cookie = 'theme=dark'; localStorage.setItem('cart', '2')</script>",
        headers=[*HTML, ("Set-Cookie", "auth=ok; HttpOnly")],
    )
    server.route("/echo", handler=_cookie_echo)
    session = Session("alice", cookies=[Cookie(name="sid", value="1", domain="127.0.0.1")])

    async with BrowserFetcher() as fetcher:
        login = await fetcher.fetch(_render(server.url("/login")), session=session)
        echo = await fetcher.fetch(_render(server.url("/echo")), session=session)

    assert (login.session, login.request.session) == ("alice", None)
    assert {cookie.name: cookie.value for cookie in session.cookies} == {"sid": "1", "auth": "ok", "theme": "dark"}
    assert next(cookie for cookie in session.cookies if cookie.name == "auth").http_only
    assert session.local_storage == {server.url("").rstrip("/"): {"cart": "2"}}
    assert echo.css("p::text").get() == "sid=1; auth=ok; theme=dark"


async def test_a_sessions_context_is_kept_for_its_next_fetch(server: LoopbackServer) -> None:
    server.route("/", "<p>page</p>", headers=HTML)

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(_render(server.url("/")), session=Session("alice"))
        kept = fetcher._contexts._waiting[0].context  # pyright: ignore[reportPrivateUsage]
        await fetcher.fetch(_render(server.url("/")), session=Session("alice"))
        again = fetcher._contexts._waiting[0].context  # pyright: ignore[reportPrivateUsage]
        await fetcher.fetch(_render(server.url("/")), session=Session("bob"))
        count = len(fetcher._contexts)  # pyright: ignore[reportPrivateUsage]

    assert again is kept
    assert count == 2


@pytest.mark.parametrize("kept", [8, 0], ids=["kept", "recreated"])
async def test_local_storage_is_the_sessions_own(server: LoopbackServer, kept: int) -> None:
    server.route("/", REMEMBER, headers=HTML)
    alice, bob = Session("alice"), Session("bob")

    async with BrowserFetcher(BrowserSettings(max_idle_contexts=kept)) as fetcher:
        first = await fetcher.fetch(_render(server.url("/")), session=alice)
        again = await fetcher.fetch(_render(server.url("/")), session=alice)
        other = await fetcher.fetch(_render(server.url("/")), session=bob)

    assert [response.css("#seen::text").get() for response in (first, again, other)] == ["first", "again", "first"]
    assert alice.local_storage == bob.local_storage == {server.url("").rstrip("/"): {"visit": "again"}}


async def test_cookie_attributes_survive_the_round_trip_through_the_browser(server: LoopbackServer) -> None:
    server.route("/x/page", handler=_cookie_echo)
    now = time.time()
    expires = float(int(now) + 3600)
    cookies = [
        Cookie(name="host", value="1", domain="app.example.localhost", created=now - 50, modified=now - 50),
        Cookie(
            name="domain",
            value="1",
            domain="example.localhost",
            host_only=False,
            created=now - 40,
            modified=now - 40,
        ),
        Cookie(
            name="scoped", value="1", domain="app.example.localhost", path="/x", created=now - 30, modified=now - 30
        ),
        Cookie(name="other-path", value="1", domain="app.example.localhost", path="/y"),
        Cookie(
            name="strict",
            value="1",
            domain="app.example.localhost",
            expires=expires,
            secure=True,
            http_only=True,
            same_site="strict",
        ),
        Cookie(name="none", value="1", domain="app.example.localhost", secure=True, same_site="none"),
        Cookie(name="unspecified", value="1", domain="app.example.localhost"),
    ]
    session = Session("alice", cookies=cookies)
    url = _on(server, "app.example.localhost", "/x/page")

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(url), session=session)

    assert list(session.cookies) == cookies  # every attribute, and when each was set, as they were
    sent = response.css("p::text").get() or ""
    assert sorted(sent.split("; ")) == sorted(
        f"{cookie.name}=1" for cookie in cookies if cookie.name not in ("other-path",)
    )


async def test_cookie_attributes_a_page_sets_are_kept(server: LoopbackServer) -> None:
    server.route(
        "/x/login",
        "<p>in</p>",
        headers=[
            *HTML,
            ("Set-Cookie", "domain=1; Domain=example.localhost; Path=/x; Max-Age=3600; HttpOnly; SameSite=Strict"),
            ("Set-Cookie", "secure=1; Secure; SameSite=None"),
            ("Set-Cookie", "plain=1"),
        ],
    )
    session = Session("alice")
    before = time.time()

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(_render(_on(server, "app.example.localhost", "/x/login")), session=session)

    held = {cookie.name: cookie for cookie in session.cookies}
    domain, secure, plain = held["domain"], held["secure"], held["plain"]
    assert (domain.domain, domain.host_only, domain.path, domain.http_only, domain.same_site) == (
        "example.localhost",
        False,
        "/x",
        True,
        "strict",
    )
    assert domain.expires == pytest.approx(before + 3600, abs=5)
    assert (secure.domain, secure.host_only, secure.path, secure.secure, secure.same_site) == (
        "app.example.localhost",
        True,
        "/x",
        True,
        "none",
    )
    assert (plain.expires, plain.same_site) == (None, "lax")  # the browser reports one without SameSite as Lax


async def test_cookies_the_page_removed_are_removed_from_the_session(server: LoopbackServer) -> None:
    server.route("/logout", "<p>out</p>", headers=[*HTML, ("Set-Cookie", "sid=; Max-Age=0")])
    session = Session("alice", cookies=[Cookie(name="sid", value="1", domain="127.0.0.1")])

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(_render(server.url("/logout")), session=session)

    assert list(session.cookies) == []
    assert ("127.0.0.1", True, "/", "sid") in session.cookies.removals


async def test_a_requests_own_cookies_go_with_it_but_are_not_kept(server: LoopbackServer) -> None:
    server.route("/echo", handler=_cookie_echo)
    session = Session("alice", cookies=[Cookie(name="sid", value="session", domain="127.0.0.1")])

    async with BrowserFetcher() as fetcher:
        own = await fetcher.fetch(_render(server.url("/echo"), cookies={"sid": "own", "extra": "1"}), session=session)
        later = await fetcher.fetch(_render(server.url("/echo")), session=session)

    assert own.css("p::text").get() == "sid=own; extra=1"
    assert later.css("p::text").get() == "sid=session"
    assert [(cookie.name, cookie.value) for cookie in session.cookies] == [("sid", "session")]


async def test_changes_made_elsewhere_reach_the_kept_context(server: LoopbackServer) -> None:
    server.route("/echo", handler=_cookie_echo)
    session = Session("alice", cookies=[Cookie(name="old", value="1", domain="127.0.0.1")])

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(_render(server.url("/echo")), session=session)
        session.cookies.discard(("127.0.0.1", True, "/", "old"))  # as an HTTP fetch of the Session did meanwhile
        session.cookies.set(Cookie(name="new", value="1", domain="127.0.0.1"))
        echo = await fetcher.fetch(_render(server.url("/echo")), session=session)

    assert echo.css("p::text").get() == "new=1"


async def test_a_kept_context_records_what_each_page_requested_from_other_hosts_once(server: LoopbackServer) -> None:
    server.route("/", "<p>shop</p><img src='https://pernix.invalid/cover.png'>", headers=HTML)
    session = Session("alice")

    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard:
            for _ in range(3):
                await fetcher.fetch(_render(server.url("/")), session=session)

    assert guard.blocked == ["https://pernix.invalid/cover.png"] * 3
