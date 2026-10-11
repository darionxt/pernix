"""Block Python-level socket connections that leave the loopback interface."""

from __future__ import annotations

import ipaddress
import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import pytest


class NetworkAccessError(RuntimeError):
    """Raised when a test tries to connect to a non-loopback address."""


def is_local(address: Any) -> bool:
    if isinstance(address, (str, bytes)):  # Unix domain socket path
        return True
    host = address[0]
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _check(address: Any) -> None:
    if not is_local(address):
        raise NetworkAccessError(f"external network access is blocked in tests: {address!r}")


def install(monkeypatch: pytest.MonkeyPatch) -> None:
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def connect(self: socket.socket, address: Any) -> None:
        _check(address)
        original_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        _check(address)
        return original_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
