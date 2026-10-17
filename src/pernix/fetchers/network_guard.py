"""Keeping Fetchers off the network, as tests need: only loopback hosts may be reached.

curl and browsers open their own connections, which tools that block Python's
sockets cannot see, so the Fetchers check the guard themselves before connecting,
and before following a redirect.
"""

from __future__ import annotations

import ipaddress
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pernix.errors import NetworkBlocked

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["NetworkGuard", "admitted", "blocking_network", "current_guard", "is_loopback_host"]


@dataclass(slots=True)
class NetworkGuard:
    """Admits only URLs on loopback hosts, or reached through a proxy on one, and records the others.

    Attributes:
        blocked: The URLs refused, in order.
    """

    blocked: list[str] = field(default_factory=list[str])

    def admits(self, url: str, *, proxy: str | None = None) -> bool:
        """Whether a URL may be fetched.

        Args:
            url: The URL to connect to.
            proxy: The proxy the connection goes through, if any.

        Returns:
            Whether the URL, or the proxy, is on a loopback host.
        """
        return admitted(url, proxy=proxy)

    def check(self, url: str, *, proxy: str | None = None) -> None:
        """Refuse a URL the guard does not admit.

        Args:
            url: The URL to connect to.
            proxy: The proxy the connection goes through, if any.

        Raises:
            pernix.errors.NetworkBlocked: The URL is not admitted; it is recorded.
        """
        if not self.admits(url, proxy=proxy):
            self.blocked.append(url)
            raise NetworkBlocked(
                f"the network guard refused {url}: only loopback hosts, such as 127.0.0.1 and localhost, "
                "may be reached while it is on"
            )


_guard: NetworkGuard | None = None


@contextmanager
def blocking_network() -> Generator[NetworkGuard]:
    """Turn the network guard on for the whole process inside the block.

    Yields:
        The guard, whose `blocked` lists the URLs it refused.
    """
    global _guard  # the guard covers every task and event loop, as the network it guards does
    previous, _guard = _guard, NetworkGuard()
    try:
        yield _guard
    finally:
        _guard = previous


def admitted(url: str, *, proxy: str | None = None) -> bool:
    """Whether a network guard admits a URL: it, or the proxy it goes through, is on a loopback host.

    Args:
        url: The URL to connect to.
        proxy: The proxy the connection goes through, if any.

    Returns:
        Whether the URL may be fetched while a guard is on.
    """
    return is_loopback_host(_host(proxy if proxy else url))


def current_guard() -> NetworkGuard | None:
    """Return the network guard in effect, or `None` if the network is open."""
    return _guard


def is_loopback_host(host: str) -> bool:
    """Whether a host name is a loopback one: a loopback IP address, `localhost`, or a `.localhost` name.

    Args:
        host: The host, as in a URL; IPv6 addresses may be bracketed.

    Returns:
        Whether connecting to it stays on this machine.
    """
    name = host.strip("[]").rstrip(".").lower()
    if name == "localhost" or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def _host(url: str) -> str:
    try:
        return urlsplit(url if "://" in url else f"//{url}").hostname or ""  # a proxy may leave out its scheme
    except ValueError:  # such as an unclosed IPv6 bracket
        return ""
