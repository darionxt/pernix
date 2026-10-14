from __future__ import annotations

import asyncio
import gzip
import socket
import time
import zlib
from typing import TYPE_CHECKING

import brotli
import orjson
import pytest
import zstandard

from loopback import ReceivedRequest, Reply, ScriptedServer
from pernix import Request
from pernix.errors import (
    ConnectError,
    DecodeError,
    FetchError,
    ProxyError,
    ResponseTooLarge,
    Timeout,
    TLSError,
    TooManyRedirects,
)
from pernix.fetchers import HTTPFetcher, HTTPSettings

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

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


async def test_bodies_that_fail_to_decompress_raise_decode_error(server: LoopbackServer) -> None:
    server.route("/bad", b"not gzip at all", headers=[("Content-Encoding", "gzip")])

    async with HTTPFetcher() as fetcher:
        with pytest.raises(DecodeError) as error:
            await fetcher.fetch(Request(server.url("/bad")))

    assert error.value.code == 61  # CURLE_BAD_CONTENT_ENCODING


def _headers_echo(request: ReceivedRequest) -> Reply:
    return Reply(body=orjson.dumps(request.headers))


async def test_request_headers_keep_their_order_case_and_duplicates(server: LoopbackServer) -> None:
    server.route("/echo", handler=_headers_echo)

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/echo"), headers=[("X-B", "1"), ("x-a", "2"), ("X-B", "3")]))

    received = server.received[-1].headers
    assert [(name, value) for name, value in received if name.lower() in ("x-a", "x-b")] == [
        ("X-B", "1"),
        ("x-a", "2"),
        ("X-B", "3"),
    ]


async def test_response_headers_keep_every_value(server: LoopbackServer) -> None:
    server.route("/multi", "ok", headers=[("X-Multi", "a"), ("X-Multi", "b")])

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/multi")))

    assert response.headers.get_all("X-Multi") == ["a", "b"]


async def test_requests_impersonate_the_pinned_profile_unless_they_name_another(server: LoopbackServer) -> None:
    server.route("/echo", handler=_headers_echo)

    async with HTTPFetcher() as fetcher:
        for impersonate in (None, "chrome136", None):
            await fetcher.fetch(Request(server.url("/echo"), impersonate=impersonate))

    agents = [request.header("User-Agent") or "" for request in server.received]
    assert ["Chrome/150.0" in agents[0], "Chrome/136.0" in agents[1], "Chrome/150.0" in agents[2]] == [True] * 3
    assert 'v="150"' in (server.received[0].header("sec-ch-ua") or "")


async def test_cleartext_requests_use_http_1_1_as_browsers_do(server: LoopbackServer) -> None:
    server.route("/echo", handler=_headers_echo)

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/echo")))

    assert response.http_version == "HTTP/1.1"
    assert server.received[-1].header("Upgrade") is None


@pytest.mark.parametrize(("profile", "problem"), [("chrome", "drifts"), ("chrome1500", "unknown Profile")])
async def test_requests_naming_an_unpinned_profile_fail(server: LoopbackServer, profile: str, problem: str) -> None:
    async with HTTPFetcher() as fetcher:
        with pytest.raises(ValueError, match=problem):
            await fetcher.fetch(Request(server.url("/"), impersonate=profile))

    assert server.received == []


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("gzip", gzip.compress),
        ("deflate", zlib.compress),
        ("br", brotli.compress),
        ("zstd", zstandard.compress),
    ],
)
async def test_encoded_bodies_are_decoded_and_lose_their_encoding_headers(
    server: LoopbackServer, encoding: str, compress: Callable[[bytes], bytes]
) -> None:
    body = b"pernix " * 200
    server.route("/encoded", compress(body), headers=[("Content-Encoding", encoding), ("X-Kept", "yes")])
    server.route("/encoded", compress(body), headers=[("Content-Encoding", encoding)], method="HEAD")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/encoded")))
        head = await fetcher.fetch(Request(server.url("/encoded"), method="HEAD"))

    assert response.body == body
    assert ("Content-Encoding" in response.headers, "Content-Length" in response.headers) == (False, False)
    assert response.headers.get("X-Kept") == "yes"
    assert (head.headers.get("Content-Encoding"), head.body) == (encoding, b"")  # nothing was decoded


async def test_a_transfer_that_stalls_times_out() -> None:
    script = [(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\na", 0.0), (b"b", 3.0)]

    with ScriptedServer(script) as stalling:
        async with HTTPFetcher() as fetcher:
            started = time.monotonic()
            with pytest.raises(Timeout) as stalled:
                await fetcher.fetch(Request(stalling.url, timeout=1))

    assert time.monotonic() - started < 2.9
    assert stalled.value.code == 28  # CURLE_OPERATION_TIMEDOUT


async def test_the_deadline_limits_the_whole_fetch() -> None:
    trickle = [(b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n", 0.0), *[(b"a", 0.1)] * 20]

    with ScriptedServer(trickle) as trickling:
        async with HTTPFetcher(HTTPSettings(deadline=0.5)) as fetcher:
            started = time.monotonic()
            with pytest.raises(Timeout, match=r"took longer than http\.deadline \(0\.5 s\)"):
                await fetcher.fetch(Request(trickling.url))

    assert time.monotonic() - started < 1.5


async def test_redirects_are_followed_up_to_the_limit(server: LoopbackServer) -> None:
    for n in range(3):
        server.route(f"/r{n}", status=302, headers=[("Location", f"/r{n + 1}")])
    server.route("/r3", "end")

    async with HTTPFetcher(HTTPSettings(max_redirects=2)) as limited, HTTPFetcher() as default:
        with pytest.raises(TooManyRedirects):
            await limited.fetch(Request(server.url("/r0")))
        response = await default.fetch(Request(server.url("/r0")))

    assert (response.text, len(response.history)) == ("end", 3)


async def test_bodies_over_the_limit_stop_the_transfer(server: LoopbackServer) -> None:
    server.route("/big", b"x" * 2000)
    unsized = [(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"y" * 3000, 0.0)]

    with ScriptedServer(unsized) as undeclared:
        async with HTTPFetcher(HTTPSettings(max_body_size=1000)) as limited:
            with pytest.raises(ResponseTooLarge, match=r"has 2000 bytes, more than 1000"):
                await limited.fetch(Request(server.url("/big")))
            with pytest.raises(ResponseTooLarge, match=r"exceeds 1000 bytes"):
                await limited.fetch(Request(undeclared.url))
        async with HTTPFetcher(HTTPSettings(max_body_size=0)) as unlimited:
            assert len((await unlimited.fetch(Request(server.url("/big")))).body) == 2000


async def test_a_407_from_a_proxy_is_a_proxy_error(server: LoopbackServer) -> None:
    server.route("/", status=407, headers=[("Proxy-Authenticate", 'Basic realm="proxy"')])

    async with HTTPFetcher() as fetcher:
        with pytest.raises(ProxyError, match="407 Proxy Authentication Required"):
            await fetcher.fetch(Request("http://pernix.test/", proxy=server.base_url))
        response = await fetcher.fetch(Request(server.url("/")))  # from the origin itself, it is a Response

    assert response.status == 407


async def test_cancelled_fetches_leave_the_connections_usable(server: LoopbackServer) -> None:
    def slow(_request: ReceivedRequest) -> Reply:
        time.sleep(1)
        return Reply(body="late")

    server.route("/slow", handler=slow)
    server.route("/fast", "fast")
    trickle = [(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n", 0.0), *[(b"a", 0.1)] * 50]

    with ScriptedServer(trickle) as trickling:
        async with HTTPFetcher(max_clients=1) as fetcher:
            for url in (server.url("/slow"), trickling.url):  # waiting for headers, then reading the body
                fetching = asyncio.create_task(fetcher.fetch(Request(url)))
                await asyncio.sleep(0.3)
                fetching.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await fetching
                async with asyncio.timeout(5):
                    assert (await fetcher.fetch(Request(server.url("/fast")))).text == "fast"


async def test_streamed_bodies_are_read_as_they_arrive_or_saved(server: LoopbackServer, tmp_path: Path) -> None:
    body = b"0123456789" * 1000
    server.route("/file", body, headers=[("Content-Type", "application/octet-stream")])

    async with HTTPFetcher() as fetcher:
        async with fetcher.stream(Request(server.url("/file"))) as streamed:
            assert (streamed.response.status, streamed.response.body) == (200, b"")
            read = b"".join([chunk async for chunk in streamed.iter_bytes()])
            with pytest.raises(RuntimeError, match="read only once"):
                await anext(streamed.iter_bytes())
        async with fetcher.stream(Request(server.url("/file"))) as streamed:
            written = await streamed.save(tmp_path / "file.bin")

    assert read == body
    assert written == len(body)
    assert _read(tmp_path / "file.bin") == body


async def test_streamed_bodies_respect_the_byte_limit(server: LoopbackServer) -> None:
    server.route("/file", b"x" * 2000)
    unsized = [(b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n" + b"y" * 3000, 0.0)]

    with ScriptedServer(unsized) as undeclared:
        async with HTTPFetcher() as fetcher:
            with pytest.raises(ResponseTooLarge, match="has 2000 bytes"):
                async with fetcher.stream(Request(server.url("/file")), max_bytes=1000):
                    pass
            async with fetcher.stream(Request(undeclared.url), max_bytes=1000) as streamed:
                with pytest.raises(ResponseTooLarge, match="exceeds 1000 bytes"):
                    async for _ in streamed.iter_bytes():
                        pass
            async with fetcher.stream(Request(server.url("/file")), max_bytes=0) as streamed:
                assert len(b"".join([chunk async for chunk in streamed.iter_bytes()])) == 2000


def _read(path: Path) -> bytes:
    return path.read_bytes()
