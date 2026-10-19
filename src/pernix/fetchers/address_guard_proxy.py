"""A SOCKS5 proxy on 127.0.0.1 that keeps a browser off hosts whose addresses are not public, for `network.public_only`.

A browser launched while `network.public_only` is on sends all its traffic to it:
the documents of its pages, their subresources, redirects, and WebSockets, and its
own requests that belong to no page. Chrome hands a SOCKS5 proxy each host as the
URL names it, never resolving it itself, so the proxy resolves it, checks every
address with the address guard (`pernix.fetchers.address_guard`), and connects
only to the addresses it checked: a name answering with a public address for the
check and a private one for the connection, as in DNS rebinding, cannot slip
through. Over SOCKS5, each connection goes to one host, and carries the bytes
Chrome would send on a connection of its own.

A connection the proxy does not make fails in the browser with
`net::ERR_SOCKS_CONNECTION_FAILED`, whatever the reason, so the proxy keeps why, by
host and port, before it answers, for the browser Fetcher to tell.
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import re
import socket
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

from curl_cffi import CurlECode

from pernix.errors import ConnectError, FetchError, NetworkBlocked, SettingsError, Timeout
from pernix.fetchers.address_guard import HostRefused, refused_hop
from pernix.fetchers.loopback_listener import LoopbackListener
from pernix.urls import url_host

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from pernix.fetchers.address_guard import AddressGuard
    from pernix.fetchers.browser_settings import BrowserSettings
    from pernix.fetchers.network_guard import NetworkGuard

__all__ = ["AddressGuardProxy", "ProxiedFailure", "check_launch_options"]

_HANDSHAKE_TIMEOUT = 10.0
"""Seconds a client has to say where it connects to."""

_CONNECT_TIMEOUT = 30.0
"""Seconds to resolve a host and connect to it, at most."""

_STAGGER = 0.25
"""Seconds a connection to one address has before the next address is tried too, as browsers do (Happy Eyeballs)."""

_CACHED_FOR = 60.0
"""Seconds a host's addresses, once checked, are connected to without resolving it again, as Chrome caches them."""

_REMEMBERED = 1024
"""The hosts whose addresses, and the hosts and ports whose latest failure and latest address, are kept, at most."""

_CHUNK = 65536

_VERSION = 5
_NO_AUTHENTICATION = 0x00
_NO_ACCEPTABLE_METHOD = 0xFF
_CONNECT = 0x01
_IPV4, _NAME, _IPV6 = 0x01, 0x03, 0x04

_SUCCEEDED = 0x00
_NOT_ALLOWED = 0x02
_HOST_UNREACHABLE = 0x04
_CONNECTION_REFUSED = 0x05
_TTL_EXPIRED = 0x06
_COMMAND_NOT_SUPPORTED = 0x07
_ADDRESS_TYPE_NOT_SUPPORTED = 0x08

_HOST_NAME = re.compile(r"[A-Za-z0-9._-]+")
"""A host name as Chrome hands it: in ASCII, an IDN as Punycode."""

_DEFAULT_PORTS = {"http": 80, "ws": 80, "https": 443, "wss": 443}

_PROXY_SWITCHES = frozenset(
    {
        "proxy-server",
        "proxy-pac-url",
        "proxy-bypass-list",
        "no-proxy-server",
        "proxy-auto-detect",
        "webrtc-ip-handling-policy",
    }
)
"""Chrome switches that would send its traffic around the proxy it is launched with, WebRTC's included."""

type _Key = tuple[str, int]


@dataclass(frozen=True, slots=True)
class ProxiedFailure:
    """Why the proxy did not connect a client to a host and port.

    Attributes:
        reason: Why, such as `127.0.0.1 is a loopback address` for a host refused.
        kind: The error it is: `NetworkBlocked` for a host refused, `ConnectError`
            or `Timeout` for one not reached.
        code: The error's code, as curl's for the same failure, or 0.
    """

    reason: str
    kind: type[FetchError]
    code: int = 0

    @property
    def refused(self) -> bool:
        """Whether the address guard refused the host."""
        return self.kind is NetworkBlocked

    def error(self, hop: str) -> FetchError:
        """The error of a hop that failed so.

        Args:
            hop: The hop, as the message names it: its URL, or the redirect that
                led to it.

        Returns:
            A `NetworkBlocked` for a host refused, as the HTTP Fetcher raises it;
            the error of its kind otherwise.
        """
        if self.refused:
            return refused_hop(hop, self.reason)
        return self.kind(f"loading {hop} failed: {self.reason}", code=self.code)


class AddressGuardProxy:
    """A SOCKS5 proxy that connects only to hosts on public addresses, at the addresses it checked.

    A host's addresses are kept for a minute once checked, as Chrome keeps those it
    resolves, so the connections a page makes to a host meanwhile go to the same
    address, and a name answering otherwise meanwhile is not asked again. Hosts of
    `network.allow_hosts` are resolved and connected to whatever their addresses.
    Under the network guard of tests, only the hosts it admits are reached,
    whatever the address guard says.
    """

    def __init__(self, guard: AddressGuard, *, network_guard: NetworkGuard | None = None) -> None:
        """Create the proxy; it listens once started.

        Args:
            guard: The address guard that checks each host.
            network_guard: The network guard of tests in effect, if any.
        """
        self._guard = guard
        self._network_guard = network_guard
        self._listener = LoopbackListener(self._serve)
        self._failures: dict[_Key, ProxiedFailure] = {}
        self._addresses: dict[_Key, str] = {}
        self._checked: dict[str, tuple[float, tuple[str, ...]]] = {}
        """The addresses of each host lately checked, with when they expire."""

    @property
    def url(self) -> str:
        """The URL of the proxy, such as `socks5://127.0.0.1:49152`.

        Raises:
            RuntimeError: The proxy is not started.
        """
        try:
            port = self._listener.port
        except RuntimeError:
            raise RuntimeError("the address guard's proxy is not started") from None
        return f"socks5://127.0.0.1:{port}"

    async def start(self) -> None:
        """Start listening on a port of its own."""
        await self._listener.start()

    async def close(self) -> None:
        """Stop listening, and close the connections still open, both ways."""
        await self._listener.close()

    def failure(self, url: str) -> ProxiedFailure | None:
        """Why the latest connection to the host and port of a URL that the proxy did not make failed.

        Args:
            url: The URL, such as one whose request failed with
                `net::ERR_SOCKS_CONNECTION_FAILED`.

        Returns:
            The failure; `None` if no connection to them failed lately.
        """
        key = _key(url)
        return None if key is None else self._failures.get(key)

    def address(self, url: str) -> str | None:
        """The address the latest connection to the host and port of a URL went to.

        Args:
            url: The URL.

        Returns:
            The address; `None` if no connection to them was made lately.
        """
        key = _key(url)
        return None if key is None else self._addresses.get(key)

    async def _serve(self, client: socket.socket) -> None:
        """Connect a client to the host it names, if allowed, then carry the bytes both ways until either side ends."""
        loop = asyncio.get_running_loop()
        client.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT):
                target = await _handshake(loop, client)
        except TimeoutError:
            return
        if target is None:
            return
        upstream, reply = await self._connect(*target)
        if upstream is None:
            await loop.sock_sendall(client, _reply(reply))
            return
        try:
            await loop.sock_sendall(client, _reply(_SUCCEEDED))
            await _splice(loop, client, upstream)
        finally:
            upstream.close()

    async def _connect(self, host: str, port: int) -> tuple[socket.socket | None, int]:
        """Connect to a host at the addresses checked, or tell why not, keeping why, with the SOCKS5 reply for it."""
        key = (host, port)
        try:
            if self._network_guard is not None and not self._network_guard.admits(_url(host, port)):
                raise HostRefused(f"the network guard admits only loopback hosts, such as localhost, not {host}")
            async with asyncio.timeout(_CONNECT_TIMEOUT):
                addresses = await self._addresses_of(host, port)
                upstream, address = await _connected(addresses, port)
        except HostRefused as exc:
            return self._failed(key, ProxiedFailure(exc.reason, NetworkBlocked), _NOT_ALLOWED)
        except ConnectError as exc:  # the host does not resolve
            return self._failed(key, ProxiedFailure(str(exc), ConnectError, exc.code), _HOST_UNREACHABLE)
        except TimeoutError:  # before OSError, which it is
            reason = f"connecting to {host} port {port} took longer than {_CONNECT_TIMEOUT:g} s"
            return self._failed(key, ProxiedFailure(reason, Timeout), _TTL_EXPIRED)
        except OSError as exc:
            reason = f"could not connect to {host} port {port}: {exc.strerror or exc}"
            failure = ProxiedFailure(reason, ConnectError, CurlECode.COULDNT_CONNECT)
            return self._failed(key, failure, _CONNECTION_REFUSED)
        _remember(self._addresses, key, address)
        return upstream, _SUCCEEDED

    async def _addresses_of(self, host: str, port: int) -> tuple[str, ...]:
        """The addresses of a host, checked: those kept, unless they expired, or those the address guard gives."""
        now = time.monotonic()
        kept = self._checked.get(host)
        if kept is not None and kept[0] > now:
            return kept[1]
        addresses = await self._guard.addresses(host, port)
        _remember(self._checked, host, (now + _CACHED_FOR, addresses))
        return addresses

    def _failed(self, key: _Key, failure: ProxiedFailure, reply: int) -> tuple[None, int]:
        _remember(self._failures, key, failure)
        return None, reply


def check_launch_options(settings: BrowserSettings) -> None:
    """Refuse launch options that would take a browser around the proxy of `network.public_only`.

    They are a proxy of the browser's own, Chrome switches that set or skip one, or
    set what WebRTC does, and leaving out the default arguments that set it.

    Args:
        settings: The browser settings.

    Raises:
        pernix.errors.SettingsError: `browser.launch_options` holds some.
    """
    found = _around_the_proxy(settings.launch_options)
    if found:
        key = settings.key("launch_options")
        raise SettingsError(
            [
                f"{key}: {option} would take the browser around the proxy that checks the hosts it reaches while "
                "network.public_only is on; leave it out"
                for option in found
            ]
        )


def _around_the_proxy(launch_options: Mapping[str, Any]) -> list[str]:
    """The launch options that would take a browser around its proxy, as error messages name them."""
    found: list[str] = []
    if "proxy" in launch_options:
        found.append("'proxy'")
    ignored: object = launch_options.get("ignore_default_args")
    if ignored is True:
        found.append("'ignore_default_args': true")
    else:
        found += [f"{arg!r} in 'ignore_default_args'" for arg in _switches(ignored)]
    found += [f"{arg!r} in 'args'" for arg in _switches(launch_options.get("args"))]
    return found


def _switches(args: object) -> list[str]:
    """The arguments of a list of them that set, or skip, a proxy, or set what WebRTC does."""
    if not isinstance(args, list | tuple):
        return []
    return [arg for arg in cast("list[object]", args) if isinstance(arg, str) and _switch(arg) in _PROXY_SWITCHES]


def _switch(arg: str) -> str:
    """The name of the Chrome switch an argument sets, such as `proxy-server`; empty for none."""
    return arg.lstrip("-").split("=", 1)[0].lower() if arg.startswith("-") else ""


async def _handshake(loop: asyncio.AbstractEventLoop, client: socket.socket) -> tuple[str, int] | None:
    """Read where a SOCKS5 client connects to; `None` once a client that cannot be served was answered."""
    version, count = await _received(loop, client, 2)
    methods = await _received(loop, client, count)
    if version != _VERSION:
        return None
    if _NO_AUTHENTICATION not in methods:
        await loop.sock_sendall(client, bytes([_VERSION, _NO_ACCEPTABLE_METHOD]))
        return None
    await loop.sock_sendall(client, bytes([_VERSION, _NO_AUTHENTICATION]))
    version, command, _reserved, kind = await _received(loop, client, 4)
    if kind == _IPV4:
        host: str | None = socket.inet_ntop(socket.AF_INET, await _received(loop, client, 4))
    elif kind == _IPV6:
        host = socket.inet_ntop(socket.AF_INET6, await _received(loop, client, 16))
    elif kind == _NAME:
        (length,) = await _received(loop, client, 1)
        host = _named_host((await _received(loop, client, length)).decode("ascii", errors="replace"))
    else:
        await loop.sock_sendall(client, _reply(_ADDRESS_TYPE_NOT_SUPPORTED))
        return None
    port = int.from_bytes(await _received(loop, client, 2), "big")
    if version != _VERSION or command != _CONNECT:
        await loop.sock_sendall(client, _reply(_COMMAND_NOT_SUPPORTED))
        return None
    if host is None:
        await loop.sock_sendall(client, _reply(_NOT_ALLOWED))
        return None
    return host, port


def _named_host(name: str) -> str | None:
    """A host as `pernix.urls.url_host()` reads it, from the name a client gave; `None` for one that is no host."""
    if ":" in name:  # an IPv6 address, as Chrome writes it
        try:
            return ipaddress.IPv6Address(name).compressed
        except ValueError:
            return None
    return name.lower() if _HOST_NAME.fullmatch(name) else None


async def _received(loop: asyncio.AbstractEventLoop, client: socket.socket, size: int) -> bytes:
    """Exactly `size` bytes from a client.

    Raises:
        ConnectionError: The client closed the connection first.
    """
    data = b""
    while len(data) < size:
        chunk = await loop.sock_recv(client, size - len(data))
        if not chunk:
            raise ConnectionError("the client closed the connection")
        data += chunk
    return data


def _reply(code: int) -> bytes:
    """A SOCKS5 reply, bound to no address in particular."""
    return bytes([_VERSION, code, 0, _IPV4, 0, 0, 0, 0, 0, 0])


async def _connected(addresses: tuple[str, ...], port: int) -> tuple[socket.socket, str]:
    """A connection to the first of the addresses that takes one, and its address.

    The next address is tried as soon as one fails, or once one has not connected
    within `_STAGGER`, the attempts under way going on, so an address that never
    answers does not keep the others from being tried. The attempts left once one
    connected, or once this is cancelled, are abandoned at once, a connection one
    makes all the same being closed.

    Raises:
        OSError: None did; the error of the last.
    """
    waiting = list(addresses)
    attempts: dict[asyncio.Task[socket.socket], str] = {}
    failure: OSError | None = None
    try:
        while waiting or attempts:
            if waiting:
                address = waiting.pop(0)
                attempts[asyncio.create_task(_attempt(address, port))] = address
            done, _pending = await asyncio.wait(
                attempts, timeout=_STAGGER if waiting else None, return_when=asyncio.FIRST_COMPLETED
            )
            for attempt in done:
                address = attempts.pop(attempt)
                try:
                    return attempt.result(), address
                except OSError as exc:
                    failure = exc
        raise failure or OSError(f"no address to connect to port {port}")
    finally:
        _abandon(attempts)  # without waiting, so that nothing interrupts it


async def _attempt(address: str, port: int) -> socket.socket:
    """A connection to an address, closed unless made."""
    upstream = socket.socket(socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM)
    try:
        upstream.setblocking(False)
        await asyncio.get_running_loop().sock_connect(upstream, (address, port))
        upstream.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except BaseException:
        upstream.close()
        raise
    return upstream


def _abandon(attempts: Iterable[asyncio.Task[socket.socket]]) -> None:
    """Stop attempts to connect, closing the connection each makes all the same, now or once it ends."""
    for attempt in attempts:
        attempt.cancel()
        attempt.add_done_callback(_close_made)


def _close_made(attempt: asyncio.Task[socket.socket]) -> None:
    if not attempt.cancelled() and attempt.exception() is None:
        attempt.result().close()


async def _splice(loop: asyncio.AbstractEventLoop, client: socket.socket, upstream: socket.socket) -> None:
    """Carry the bytes of a connection both ways until both sides ended, or either broke."""

    async def carry(source: socket.socket, sink: socket.socket) -> None:
        try:
            while data := await loop.sock_recv(source, _CHUNK):
                await loop.sock_sendall(sink, data)
            sink.shutdown(socket.SHUT_WR)
        except OSError:  # either side broke: so does the connection, both ways
            for each in (source, sink):
                with contextlib.suppress(OSError):
                    each.shutdown(socket.SHUT_RDWR)

    await asyncio.gather(carry(client, upstream), carry(upstream, client))


def _remember[K, V](kept: dict[K, V], key: K, value: V) -> None:
    """Keep the latest value for a key, forgetting the oldest key beyond `_REMEMBERED`."""
    kept.pop(key, None)
    kept[key] = value
    if len(kept) > _REMEMBERED:
        del kept[next(iter(kept))]


def _key(url: str) -> _Key | None:
    """The host and port a client connects to for a URL, as the proxy keeps them."""
    host = url_host(url)
    if host is None:
        return None
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        return None
    return host, port or _DEFAULT_PORTS.get(parts.scheme.lower(), 80)


def _url(host: str, port: int) -> str:
    """A URL naming a host and port alone, for the network guard."""
    return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"
