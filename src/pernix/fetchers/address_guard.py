"""Refusing hosts whose addresses are not public, as `network.public_only` asks, for fetching untrusted links.

A host name is checked once resolved, and the client then connects only to the
addresses checked, so that a name answering with a public address for the check and
a private one for the connection, as in DNS rebinding, cannot slip through.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from curl_cffi import CurlECode

from pernix.errors import ConnectError, NetworkBlocked
from pernix.urls import is_loopback_host, url_host

__all__ = ["AddressGuard", "PinnedHost", "is_public_address"]

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
"""The well-known prefix of NAT64, whose addresses stand for the IPv4 address in their last 32 bits."""

_DEFAULT_PORTS = {"http": 80, "https": 443}


def is_public_address(address: IPAddress) -> bool:
    """Whether an address is publicly routable.

    Private, loopback, link-local, unspecified, shared (100.64.0.0/10), reserved,
    documentation, and multicast addresses are not; neither is an IPv6 address
    standing for an IPv4 one that is not, such as `::ffff:127.0.0.1` or the NAT64
    address `64:ff9b::7f00:1`.

    Args:
        address: The address.

    Returns:
        Whether hosts on the Internet at large may have it.
    """
    address = _embedded(address)
    return address.is_global and not address.is_multicast


@dataclass(frozen=True, slots=True)
class PinnedHost:
    """A host name, resolved and checked, and the addresses to connect to.

    Attributes:
        host: The host, as `pernix.urls.url_host()` reads it.
        port: The port connected to.
        addresses: The addresses it resolved to, each public, in the resolver's order.
    """

    host: str
    port: int
    addresses: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AddressGuard:
    """Refuses URLs whose hosts are not on public addresses.

    Attributes:
        allow_hosts: Host names admitted whatever their addresses, as
            `pernix.urls.url_host()` reads them.
    """

    allow_hosts: frozenset[str] = frozenset()

    async def check(
        self, url: str, *, proxy: str | None = None, redirected_from: str | None = None
    ) -> PinnedHost | None:
        """Refuse a URL whose host is not on public addresses.

        A host written as an address is checked as it is, and a loopback name, such
        as `localhost`, is refused, as clients resolve those themselves. Any other
        name is resolved, and every address it resolves to must be public. Through a
        proxy, which resolves names itself, names are not resolved.

        Args:
            url: The URL to connect to.
            proxy: The proxy the connection goes through, if any.
            redirected_from: The URL that redirected to it, if any, for the error.

        Returns:
            The host and the addresses to connect to, once it was resolved; `None`
            when there is nothing to connect to in its place: the host is allowed,
            written as an address, or reached through a proxy.

        Raises:
            pernix.errors.NetworkBlocked: The host is not on public addresses.
            pernix.errors.ConnectError: The host cannot be resolved (code 6, as curl's).
        """
        host = url_host(url)
        if host is None:
            raise _refused(url, redirected_from, "its host cannot be read")
        if host in self.allow_hosts:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None:
            if not is_public_address(address):
                raise _refused(url, redirected_from, f"{host} is {_kind(address)}")
            return None
        if is_loopback_host(host):
            raise _refused(url, redirected_from, f"{host} is a loopback host")
        if proxy:
            return None
        port = _port(url)
        addresses = await _resolve(host, port)
        for found in addresses:
            resolved = ipaddress.ip_address(found)
            if not is_public_address(resolved):
                raise _refused(url, redirected_from, f"{host} resolves to {found}, {_kind(resolved)}")
        return PinnedHost(host, port, addresses)


async def _resolve(host: str, port: int) -> tuple[str, ...]:
    """The addresses a host name resolves to, each once, in the resolver's order."""
    try:
        found = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ConnectError(f"could not resolve {host}: {exc}", code=CurlECode.COULDNT_RESOLVE_HOST) from exc
    addresses = tuple(dict.fromkeys(str(sockaddr[0]) for *_, sockaddr in found))
    if not addresses:
        raise ConnectError(f"could not resolve {host}: no address", code=CurlECode.COULDNT_RESOLVE_HOST)
    return addresses


def _embedded(address: IPAddress) -> IPAddress:
    """The IPv4 address an IPv6 address stands for, if any: mapped, or NAT64; the address itself otherwise."""
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64:
            return ipaddress.IPv4Address(int(address) & 0xFFFF_FFFF)
    return address


def _kind(address: IPAddress) -> str:
    """What makes an address not public, for error messages."""
    address = _embedded(address)
    if address.is_loopback:
        return "a loopback address"
    if address.is_link_local:
        return "a link-local address"
    if address.is_unspecified:
        return "an unspecified address"
    if address.is_multicast:
        return "a multicast address"
    if address.is_private:
        return "a private address"
    return "an address that is not publicly routable"


def _port(url: str) -> int:
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = None  # curl refuses the URL itself
    return port or _DEFAULT_PORTS.get(parts.scheme.lower(), 80)


def _refused(url: str, redirected_from: str | None, reason: str) -> NetworkBlocked:
    hop = url if redirected_from is None else f"the redirect from {redirected_from} to {url}"
    return NetworkBlocked(f"network.public_only refused {hop}: {reason}")
