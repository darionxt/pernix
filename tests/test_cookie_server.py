"""The loopback server that records the cookie fields of each request, once a test is done with it."""

from __future__ import annotations

import socket

import pytest
import trustme

from cookie_server import CookieServer


@pytest.mark.parametrize("address", ["127.0.0.1", "::1"])
def test_a_stopped_server_refuses_connections(address: str) -> None:
    try:
        server = CookieServer(trustme.CA(), address)
    except OSError:
        pytest.skip("the IPv6 loopback address is not available")
    with server:
        port = server.port

    # One still listening takes a connection meant for a later test's server on its port, answering as itself.
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection((address, port), timeout=5).close()
