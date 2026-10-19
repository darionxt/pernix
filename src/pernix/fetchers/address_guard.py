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

from ada_url import URL
from curl_cffi import CurlECode

from pernix.errors import ConnectError, NetworkBlocked
from pernix.urls import is_loopback_host, url_host

__all__ = ["AddressGuard", "HostRefused", "PinnedHost", "is_public_address", "refused_hop"]

type IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_NAT64 = ipaddress.IPv6Network("64:ff9b::/96")
"""The well-known prefix of NAT64, whose addresses stand for the IPv4 address in their last 32 bits."""

_DEFAULT_PORTS = {"http": 80, "https": 443}

_LOCALLY_RESOLVING_PROXIES = frozenset({"socks4", "socks5"})
"""Proxy schemes for which curl resolves host names itself, handing the proxy an address; `socks4a` and `socks5h` do not."""


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


class HostRefused(Exception):
    """A host the address guard refuses, before it is put in a `NetworkBlocked` naming the URL.

    Attributes:
        reason: Why, as `NetworkBlocked` messages tell it, such as
            `127.0.0.1 is a loopback address`.
    """

    def __init__(self, reason: str) -> None:
        """Create the refusal.

        Args:
            reason: Why the host is refused.
        """
        super().__init__(reason)
        self.reason = reason


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

        A host written as an address is checked as it is, in any of the forms browsers
        and curl read addresses in, such as `127.1` or `0x7f000001`, and a loopback
        name, such as `localhost`, is refused, as clients resolve those themselves. Any
        other name is resolved, and every address it resolves to must be public.
        Through a proxy, which resolves names itself, names are not resolved, but
        through a `socks4://` or `socks5://` one, curl resolves them, so they are. A
        host WHATWG URLs reject is refused.

        Args:
            url: The URL to connect to.
            proxy: The proxy the connection goes through, if any.
            redirected_from: The URL that redirected to it, if any, for the error.

        Returns:
            The host and the addresses to connect to, once it was resolved; `None`
            when there is nothing to connect to in its place: the host is allowed,
            written as an address, or resolved by the proxy.

        Raises:
            pernix.errors.NetworkBlocked: The host is not on public addresses.
            pernix.errors.ConnectError: The host cannot be resolved (code 6, as curl's).
        """
        host = url_host(url)
        try:
            if host is None:
                raise HostRefused("its host cannot be read")
            if host in self.allow_hosts or _written_public(host) is not None:
                return None
            if proxy and not _resolves_locally(proxy):
                return None
            port = _port(url)
            return PinnedHost(host, port, await _public_addresses(host, port))
        except HostRefused as exc:
            raise refused_hop(_hop(url, redirected_from), exc.reason) from None

    def check_written(self, url: str) -> None:
        """Refuse a URL whose host is written as an address that is not public, or is a loopback name, without resolving it.

        Args:
            url: The URL to connect to.

        Raises:
            pernix.errors.NetworkBlocked: The host is refused as it is written.
        """
        host = url_host(url)
        try:
            if host is None:
                raise HostRefused("its host cannot be read")
            if host not in self.allow_hosts:
                _written_public(host)
        except HostRefused as exc:
            raise refused_hop(url, exc.reason) from None

    async def addresses(self, host: str, port: int) -> tuple[str, ...]:
        """The addresses to connect to for a host, checked: those it is written as, or those it resolves to.

        An allowed host is resolved, but its addresses are not checked.

        Args:
            host: The host, as `pernix.urls.url_host()` reads it.
            port: The port to connect to.

        Returns:
            The addresses, in the resolver's order.

        Raises:
            HostRefused: The host is not on public addresses.
            pernix.errors.ConnectError: The host cannot be resolved (code 6, as curl's).
        """
        if host in self.allow_hosts:
            return await _resolve(host, port)
        address = _written_public(host)
        if address is not None:
            return (str(address),)
        return await _public_addresses(host, port)


def refused_hop(hop: str, reason: str) -> NetworkBlocked:
    """The error of a hop the address guard refused.

    Args:
        hop: The hop, as the message names it: its URL, or the redirect that led to it.
        reason: Why it was refused, as `HostRefused` tells it.

    Returns:
        The error, which says `network.public_only` refused the hop.
    """
    return NetworkBlocked(f"network.public_only refused {hop}: {reason}")


def _written_public(host: str) -> IPAddress | None:
    """The public address a host is written as; `None` for a name that is not a loopback one.

    Raises:
        HostRefused: The host is an address that is not public, a loopback name,
            or a host WHATWG URLs reject.
    """
    try:
        address = _written_address(host)
    except ValueError:
        raise HostRefused(f"{host} is not a valid host") from None
    if address is not None:
        if not is_public_address(address):
            written = host if str(address) == host else f"{host}, which is {address},"
            raise HostRefused(f"{written} is {_kind(address)}")
        return address
    if is_loopback_host(host):
        raise HostRefused(f"{host} is a loopback host")
    return None


async def _public_addresses(host: str, port: int) -> tuple[str, ...]:
    """The addresses a name resolves to, each having to be public.

    Raises:
        HostRefused: One of them is not public.
        pernix.errors.ConnectError: The name cannot be resolved.
    """
    addresses = await _resolve(host, port)
    for found in addresses:
        resolved = ipaddress.ip_address(found)
        if not is_public_address(resolved):
            raise HostRefused(f"{host} resolves to {found}, {_kind(resolved)}")
    return addresses


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


def _written_address(host: str) -> IPAddress | None:
    """The address a host is written as, read as WHATWG URLs and curl read addresses; `None` for a name.

    Raises:
        ValueError: WHATWG URLs reject the host, such as `10.0.0.256`.
    """
    if ":" in host:  # an IPv6 address, without its brackets
        return ipaddress.ip_address(host)
    hostname = URL(f"http://{host}/").hostname  # 127.1, 2130706433, and 0x7f000001 are all 127.0.0.1
    try:
        return ipaddress.ip_address(hostname)
    except ValueError:
        return None


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


def _hop(url: str, redirected_from: str | None) -> str:
    """A hop as error messages name it: its URL, or the redirect that led to it."""
    return url if redirected_from is None else f"the redirect from {redirected_from} to {url}"


def _resolves_locally(proxy: str) -> bool:
    """Whether curl resolves host names itself for a proxy, handing it the address."""
    scheme = urlsplit(proxy if "://" in proxy else f"http://{proxy}").scheme
    return scheme.lower() in _LOCALLY_RESOLVING_PROXIES
