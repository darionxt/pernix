"""The guard that keeps the suite's own Python sockets off the external network."""

from __future__ import annotations

import socket

import pytest

from netguard import NetworkAccessError


def test_external_network_is_blocked() -> None:
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(NetworkAccessError),
    ):
        sock.connect(("192.0.2.1", 80))
