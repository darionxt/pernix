from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import pytest
from curl_cffi.requests.exceptions import Timeout

from loopback import ReceivedRequest, Reply
from pernix import Request
from pernix.fetchers import HTTPFetcher

if TYPE_CHECKING:
    from loopback import LoopbackServer


def _cookie_echo(request: ReceivedRequest) -> Reply:
    return Reply(body=request.header("Cookie") or "")


async def test_request_cookies_are_sent(server: LoopbackServer) -> None:
    server.route("/echo", handler=_cookie_echo)

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/echo"), cookies={"session": "abc", "lang": "en"}))

    assert sorted(response.text.split("; ")) == ["lang=en", "session=abc"]


async def test_cookies_set_by_one_response_never_reach_a_later_request(server: LoopbackServer) -> None:
    server.route("/set", "ok", headers=[("Set-Cookie", "token=secret; Path=/")])
    server.route("/echo", handler=_cookie_echo)

    async with HTTPFetcher(max_clients=1) as fetcher:  # one handle and connection, reused for both
        await fetcher.fetch(Request(server.url("/set")))
        response = await fetcher.fetch(Request(server.url("/echo")))

    assert response.text == ""
    assert [r.header("Cookie") for r in server.received] == [None, None]


async def test_cookies_flow_within_a_redirect_chain_only(server: LoopbackServer) -> None:
    server.route(
        "/login",
        status=302,
        headers=[("Location", server.url("/home")), ("Set-Cookie", "auth=ok; Path=/")],
    )
    server.route("/home", handler=_cookie_echo)

    async with HTTPFetcher(max_clients=1) as fetcher:
        redirected = await fetcher.fetch(Request(server.url("/login")))
        later = await fetcher.fetch(Request(server.url("/home")))

    assert (redirected.url, redirected.text) == (server.url("/home"), "auth=ok")
    assert later.text == ""


async def test_a_request_timeout_applies_to_that_request(server: LoopbackServer) -> None:
    def slow(request: ReceivedRequest) -> Reply:
        time.sleep(1)
        return Reply(body="late")

    server.route("/slow", handler=slow)

    async with HTTPFetcher() as fetcher:
        started = asyncio.get_running_loop().time()
        with pytest.raises(Timeout):
            await fetcher.fetch(Request(server.url("/slow"), timeout=0.2))

    assert asyncio.get_running_loop().time() - started < 0.9


async def test_requests_for_other_fetchers_are_rejected(server: LoopbackServer) -> None:
    async with HTTPFetcher() as fetcher:
        with pytest.raises(ValueError, match="needs a Fetcher other than HTTP"):
            await fetcher.fetch(Request(server.url("/"), render=True))
        with pytest.raises(ValueError, match="needs a Fetcher other than HTTP"):
            await fetcher.fetch(Request(server.url("/"), fetcher="ftp"))

    assert server.received == []


async def test_response_cookies_hold_everything_set_during_the_transfer(server: LoopbackServer) -> None:
    server.route(
        "/login",
        status=302,
        headers=[("Location", server.url("/home")), ("Set-Cookie", "auth=ok; Path=/"), ("Set-Cookie", "tmp=1")],
    )
    server.route("/home", "home", headers=[("Set-Cookie", "theme=dark"), ("Set-Cookie", "tmp=; Max-Age=0")])
    server.route("/account", handler=_cookie_echo)

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/login"), cookies={"lang": "en"}))
        follow_up = await fetcher.fetch(Request(server.url("/account"), cookies=response.cookies))

    assert dict(response.cookies) == {"lang": "en", "auth": "ok", "theme": "dark"}
    assert sorted(follow_up.text.split("; ")) == ["auth=ok", "lang=en", "theme=dark"]


async def test_response_cookies_include_those_sent_in_a_cookie_header(server: LoopbackServer) -> None:
    server.route("/", "ok", headers=[("Set-Cookie", "theme=dark")])

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/"), headers={"Cookie": "session=abc; lang=en"}))

    assert dict(response.cookies) == {"session": "abc", "lang": "en", "theme": "dark"}
