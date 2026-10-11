"""Tests for the loopback server and the network guard used by the test suite."""

from __future__ import annotations

import asyncio
import socket
import urllib.error
import urllib.request

import pytest

from loopback import LoopbackServer, ReceivedRequest, Reply
from netguard import NetworkAccessError


def _get(url: str, data: bytes | None = None, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, data=data, headers=headers or {})
    return urllib.request.urlopen(request, timeout=5)


def test_server_serves_registered_route(server: LoopbackServer) -> None:
    server.route("/page", "<h1>hello</h1>", headers=[("Content-Type", "text/html")])

    with _get(server.url("/page?q=1")) as response:
        assert response.status == 200
        assert response.headers["Content-Type"] == "text/html"
        assert response.read() == b"<h1>hello</h1>"

    [received] = server.received
    assert received.method == "GET"
    assert received.path == "/page"
    assert received.query == [("q", "1")]


def test_server_returns_404_for_unknown_route(server: LoopbackServer) -> None:
    with pytest.raises(urllib.error.HTTPError) as excinfo:
        _get(server.url("/missing"))
    with excinfo.value as error:
        assert error.code == 404


def test_server_records_body_and_headers_in_order(server: LoopbackServer) -> None:
    server.route("/submit", method="POST", status=201)

    with _get(server.url("/submit"), data=b"payload", headers={"X-B": "2", "X-A": "1"}) as response:
        assert response.status == 201

    [received] = server.received
    assert received.method == "POST"
    assert received.body == b"payload"
    names = [name for name, _ in received.headers]
    assert names.index("X-B") < names.index("X-A")


def test_server_delegates_to_handler(server: LoopbackServer) -> None:
    def echo(request: ReceivedRequest) -> Reply:
        return Reply(body=request.header("X-Echo") or "", status=202)

    server.route("/echo", handler=echo)

    with _get(server.url("/echo"), headers={"X-Echo": "ping"}) as response:
        assert response.status == 202
        assert response.read() == b"ping"


async def test_async_tests_run_on_an_event_loop(server: LoopbackServer) -> None:
    server.route("/", "ok")

    def fetch() -> bytes:
        with _get(server.url()) as response:
            return response.read()

    assert await asyncio.to_thread(fetch) == b"ok"


def test_external_network_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessError),
    ):
        sock.connect(("192.0.2.1", 80))
