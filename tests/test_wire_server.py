"""The loopback server that records what clients put on the wire, against the HTTP Fetcher."""

from __future__ import annotations

from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from pernix import Request, Session
from pernix.fetchers import HTTPFetcher
from wire_server import HOST, WireServer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> trustme.CA:
    """A certificate authority that curl trusts, and that the server's certificate comes from."""
    ca = trustme.CA()
    bundle = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(bundle))
    monkeypatch.setattr(curl_cffi.aio, "DEFAULT_CACERT", str(bundle))  # read by each new async curl session
    return ca


@pytest.fixture
def server(ca: trustme.CA) -> Iterator[WireServer]:
    with WireServer(ca) as server:
        yield server


async def test_an_http2_connection_is_recorded_from_its_client_hello_to_each_header_block(server: WireServer) -> None:
    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/page?q=1")))

    assert (response.status, response.http_version) == (200, "HTTP/2")
    assert response.css("p::text").get() == "page"
    [connection] = server.connections
    assert connection.protocol == "HTTP/2"
    assert connection.client_hello is not None
    server_name = connection.client_hello.extension("server_name")
    assert server_name is not None
    assert server_name.data.endswith(HOST.encode())
    assert [frame.name for frame in connection.frames[:3]] == ["SETTINGS", "WINDOW_UPDATE", "HEADERS"]
    [request] = connection.requests
    assert request.path == "/page"
    assert request.headers[:4] == [
        (":method", "GET"),
        (":authority", f"{HOST}:{server.port}"),
        (":scheme", "https"),
        (":path", "/page?q=1"),
    ]
    assert request.block is not None
    assert len(request.block.representations) == len(request.headers)


async def test_cleartext_http1_requests_are_recorded_as_received(server: WireServer) -> None:
    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/plain", scheme="http"), headers={"X-Mixed-Case": "Kept"}))
        await fetcher.fetch(Request(server.url("/again", scheme="http")))

    [connection] = server.connections
    assert (connection.protocol, connection.client_hello) == ("HTTP/1.1", None)
    assert [request.request_line for request in connection.requests] == ["GET /plain HTTP/1.1", "GET /again HTTP/1.1"]
    assert ("X-Mixed-Case", "Kept") in connection.requests[0].headers
    assert connection.requests[0].headers[0] == ("Host", f"{HOST}:{server.port}")


async def test_http1_over_tls_is_recorded_when_the_server_offers_only_it(ca: trustme.CA) -> None:
    with WireServer(ca, protocols=("http/1.1",)) as server:
        async with HTTPFetcher() as fetcher:
            response = await fetcher.fetch(Request(server.url("/")))

    assert response.http_version == "HTTP/1.1"
    [connection] = server.connections
    assert connection.protocol == "HTTP/1.1"
    assert connection.client_hello is not None
    assert connection.requests[0].request_line == "GET / HTTP/1.1"


@pytest.mark.parametrize("scheme", ["https", "http"])
async def test_a_closing_path_makes_the_next_request_connect_again(server: WireServer, scheme: str) -> None:
    server.closing.add("/one")
    session = Session()

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/one", scheme=scheme)), session=session)
        await server.closed_after("/one")
        await fetcher.fetch(Request(server.url("/two", scheme=scheme)), session=session)

    first, second = server.connections
    assert [request.path for request in first.requests] == ["/one"]
    assert [request.path for request in second.requests] == ["/two"]
    assert (first.resumed, second.resumed) == ((False, True) if scheme == "https" else (False, False))
