from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING

import pytest

from loopback import ReceivedRequest, Reply
from pernix import Request
from pernix.errors import ConnectError, FetchError, Timeout, TLSError, TooManyRedirects
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


async def test_an_oversized_max_age_does_not_fail_the_fetch(server: LoopbackServer) -> None:
    server.route("/", "ok", headers=[("Set-Cookie", f"a=1; Max-Age={'9' * 5000}")])

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/")))

    assert (response.status, dict(response.cookies)) == (200, {"a": "1"})


async def test_responses_record_the_redirect_chain_and_transfer_metadata(server: LoopbackServer) -> None:
    server.route("/a", status=301, headers=[("Location", "/b"), ("X-Hop", "a")])
    server.route("/b", status=302, headers=[("Location", "/c"), ("X-Hop", "b")])
    server.route("/c", "done")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/a")))

    assert (response.url, response.status, response.text) == (server.url("/c"), 200, "done")
    assert [(hop.url, hop.status, hop.headers.get("X-Hop")) for hop in response.history] == [
        (server.url("/a"), 301, "a"),
        (server.url("/b"), 302, "b"),
    ]
    assert (response.fetcher, response.http_version, response.ip_address) == ("http", "HTTP/1.1", "127.0.0.1")
    assert response.elapsed is not None
    assert response.elapsed > 0


async def test_responses_use_the_configured_default_encoding(server: LoopbackServer) -> None:
    body = ("<html><body>" + "中文内容\uff0c这是一个测试。" * 20 + "</body></html>").encode()
    server.route("/page", body[:100] + b"\xff" + body[100:])

    async with HTTPFetcher(default_encoding="gb18030") as fetcher:
        response = await fetcher.fetch(Request(server.url("/page")))

    assert (response.encoding, response.encoding_source) == ("gb18030", "default")


async def test_transport_failures_become_fetch_errors_with_the_curl_code(server: LoopbackServer) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    server.route("/loop", status=302, headers=[("Location", "/loop")])

    async with HTTPFetcher() as fetcher:
        with pytest.raises(ConnectError) as refused:
            await fetcher.fetch(Request(f"http://127.0.0.1:{closed_port}/"))
        with pytest.raises(TLSError):
            await fetcher.fetch(Request(server.url("/").replace("http://", "https://")))
        with pytest.raises(TooManyRedirects):
            await fetcher.fetch(Request(server.url("/loop")))

    assert refused.value.code == 7  # CURLE_COULDNT_CONNECT
    assert isinstance(refused.value, FetchError)
    assert refused.value.__cause__ is not None
