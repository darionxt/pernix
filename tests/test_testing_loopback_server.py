"""The loopback server of pernix.testing."""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

import pytest

from pernix.testing import LoopbackServer, ReceivedRequest, Reply

if TYPE_CHECKING:
    from pathlib import Path


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


def test_routes_may_be_given_up_front() -> None:
    def echo(request: ReceivedRequest) -> Reply:
        return Reply(body=request.header("X-Echo") or "")

    routes = {"/text": "<p>text</p>", "/bytes": b"\x00\x01", "/reply": Reply(status=204), "/echo": echo}
    with LoopbackServer(routes) as server:
        with _get(server.url("/text")) as text, _get(server.url("/bytes")) as raw:
            assert (text.read(), raw.read()) == (b"<p>text</p>", b"\x00\x01")
        with _get(server.url("/reply")) as reply:
            assert reply.status == 204
        with _get(server.url("/echo"), headers={"X-Echo": "ping"}) as echoed:
            assert echoed.read() == b"ping"


def test_a_directory_is_served_below_the_routes(tmp_path: Path) -> None:
    site = tmp_path / "site"
    (site / "books").mkdir(parents=True)
    (site / "index.html").write_text("<h1>home</h1>")
    (site / "books" / "index.html").write_text("<h1>books</h1>")
    (site / "books" / "dune.json").write_text('{"title": "Dune"}')
    (tmp_path / "secret.txt").write_text("secret")

    with LoopbackServer({"/books/dune.json": "routed"}, directory=site) as server:
        with _get(server.url("/")) as home, _get(server.url("/books/")) as books:
            assert (home.read(), books.headers["Content-Type"]) == (b"<h1>home</h1>", "text/html")
            assert books.read() == b"<h1>books</h1>"
        with _get(server.url("/books/dune.json")) as routed:
            assert routed.read() == b"routed"
        for missing in ("/books/missing.html", "/../secret.txt", "/%2e%2e/secret.txt"):
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                _get(server.url(missing))
            with excinfo.value as error:
                assert error.code == 404
