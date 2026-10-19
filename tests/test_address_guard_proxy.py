"""The SOCKS5 proxy that keeps a browser off hosts whose addresses are not public, for `network.public_only`."""

from __future__ import annotations

import asyncio
import socket
import struct
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from pernix.errors import ConnectError, NetworkBlocked, SettingsError, Timeout
from pernix.fetchers import BrowserFetcher, BrowserSettings, address_guard_proxy
from pernix.fetchers.address_guard import AddressGuard
from pernix.fetchers.address_guard_proxy import AddressGuardProxy, ProxiedFailure
from pernix.fetchers.network_guard import NetworkGuard
from pernix.settings import NetworkSettings

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

SUCCEEDED, NOT_ALLOWED, HOST_UNREACHABLE, CONNECTION_REFUSED, COMMAND_NOT_SUPPORTED = 0, 2, 4, 5, 7


@asynccontextmanager
async def _proxy(guard: AddressGuard, *, network_guard: NetworkGuard | None = None) -> AsyncIterator[AddressGuardProxy]:
    proxy = AddressGuardProxy(guard, network_guard=network_guard)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()


@asynccontextmanager
async def _echo() -> AsyncIterator[tuple[int, list[bytes]]]:
    """A server on 127.0.0.1 that sends back what it receives, telling its port and what each connection sent first."""
    received: list[bytes] = []

    async def echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        first = await reader.read(1024)
        received.append(first)
        writer.write(first)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(echo, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1], received
    finally:
        server.close()
        server.close_clients()
        await server.wait_closed()


async def _socks5(
    proxy: AddressGuardProxy, host: str, port: int, *, command: int = 1, methods: bytes = b"\x00"
) -> tuple[int, asyncio.StreamReader, asyncio.StreamWriter]:
    """Ask the proxy to connect to a host, by name, as Chrome does: the reply's code, and the connection."""
    parts = urlsplit(proxy.url)
    reader, writer = await asyncio.open_connection(parts.hostname, parts.port)
    writer.write(bytes([5, len(methods)]) + methods)
    version, method = await reader.readexactly(2)
    assert version == 5
    if method == 0xFF:
        return method, reader, writer
    name = host.encode()
    writer.write(bytes([5, command, 0, 3, len(name)]) + name + struct.pack("!H", port))
    reply = await reader.readexactly(10)
    return reply[1], reader, writer


async def _closed(writer: asyncio.StreamWriter) -> None:
    writer.close()
    await writer.wait_closed()


async def test_an_allowed_host_is_connected_to_and_carried_both_ways() -> None:
    async with _echo() as (port, received), _proxy(AddressGuard(frozenset({"localhost"}))) as proxy:
        code, reader, writer = await _socks5(proxy, "localhost", port)
        writer.write(b"ping")
        assert await reader.read() == b"ping"  # echoed, then closed by the server
        await _closed(writer)

    assert (code, received) == (SUCCEEDED, [b"ping"])
    assert proxy.address(f"http://localhost:{port}/") in {"127.0.0.1", "::1"}
    assert proxy.failure(f"http://localhost:{port}/") is None


@pytest.mark.parametrize(
    ("host", "reason"),
    [
        ("127.0.0.1", "127.0.0.1 is a loopback address"),
        ("::1", "::1 is a loopback address"),
        ("localhost", "localhost is a loopback host"),
        ("0x7f000001", "0x7f000001, which is 127.0.0.1, is a loopback address"),
    ],
)
async def test_hosts_that_are_not_public_are_refused_without_connecting(host: str, reason: str) -> None:
    async with _echo() as (port, received), _proxy(AddressGuard()) as proxy:
        code, _reader, writer = await _socks5(proxy, host, port)
        await _closed(writer)

    assert (code, received) == (NOT_ALLOWED, [])
    url = f"ws://[{host}]:{port}/" if ":" in host else f"ws://{host}:{port}/"
    assert proxy.failure(url) == ProxiedFailure(reason, NetworkBlocked)
    assert proxy.address(url) is None


async def test_a_name_is_resolved_once_and_connected_to_at_the_address_checked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers = [["127.0.0.1"], ["10.0.0.5"]]  # public, as far as this test goes, then private

    def getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object) -> list[object]:
        assert host == "rebind.example"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers.pop(0)[0], port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr("pernix.fetchers.address_guard.is_public_address", lambda address: str(address) == "127.0.0.1")
    monkeypatch.setattr("pernix.fetchers.address_guard_proxy._CACHED_FOR", 0.0)  # asked again at once

    async with _echo() as (port, received), _proxy(AddressGuard()) as proxy:
        first, reader, writer = await _socks5(proxy, "rebind.example", port)
        writer.write(b"ping")
        assert await reader.read() == b"ping"
        await _closed(writer)
        second, _reader, writer = await _socks5(proxy, "rebind.example", port)
        await _closed(writer)

    assert (first, second, received) == (SUCCEEDED, NOT_ALLOWED, [b"ping"])
    assert proxy.address(f"http://rebind.example:{port}/") == "127.0.0.1"
    failure = proxy.failure(f"http://rebind.example:{port}/")
    assert failure == ProxiedFailure("rebind.example resolves to 10.0.0.5, a private address", NetworkBlocked)


async def test_the_addresses_checked_are_kept_for_a_while(monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[str] = []

    def getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object) -> list[object]:
        asked.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    async with _echo() as (port, received), _proxy(AddressGuard(frozenset({"shop.example"}))) as proxy:
        for text in (b"one", b"two"):
            code, reader, writer = await _socks5(proxy, "shop.example", port)
            writer.write(text)
            assert await reader.read() == text
            await _closed(writer)
            assert code == SUCCEEDED

    assert (asked, received) == (["shop.example"], [b"one", b"two"])


async def test_an_address_that_never_answers_does_not_keep_the_next_from_being_tried(
    resolver: Callable[[Mapping[str, list[str]]], None], monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver({"dual.example": ["198.51.100.1", "127.0.0.1"]})
    attempt = address_guard_proxy._attempt  # pyright: ignore[reportPrivateUsage]
    abandoned: list[str] = []

    async def unanswered(address: str, port: int) -> socket.socket:
        if address != "198.51.100.1":
            return await attempt(address, port)
        try:
            await asyncio.Event().wait()  # as a host dropping the connection attempt
        except asyncio.CancelledError:
            abandoned.append(address)
            raise
        raise AssertionError("never answered")

    monkeypatch.setattr(address_guard_proxy, "_attempt", unanswered)

    async with _echo() as (port, received), _proxy(AddressGuard(frozenset({"dual.example"}))) as proxy:
        async with asyncio.timeout(5):  # well before the 30 s connecting may take
            code, reader, writer = await _socks5(proxy, "dual.example", port)
            writer.write(b"ping")
            assert await reader.read() == b"ping"
        await _closed(writer)

    assert (code, received, abandoned) == (SUCCEEDED, [b"ping"], ["198.51.100.1"])
    assert proxy.address(f"http://dual.example:{port}/") == "127.0.0.1"


class _Made:
    """Stands in for a connection made, telling when it is closed."""

    def __init__(self, address: str, closed: list[str], closing: asyncio.Event) -> None:
        self.address = address
        self._closed = closed
        self._closing = closing

    def close(self) -> None:
        self._closed.append(self.address)
        self._closing.set()


async def test_a_race_to_connect_leaves_no_connection_open_whenever_it_is_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed: list[str] = []
    closing = asyncio.Event()
    made = asyncio.Event()

    async def attempt(address: str, _port: int) -> _Made:
        if address == "203.0.113.1":
            made.set()
            return _Made(address, closed, closing)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await asyncio.sleep(1)  # connects all the same, as the cancellation came too late
        return _Made(address, closed, closing)

    monkeypatch.setattr(address_guard_proxy, "_attempt", attempt)
    monkeypatch.setattr(address_guard_proxy, "_STAGGER", 0.0)
    connected = address_guard_proxy._connected  # pyright: ignore[reportPrivateUsage]
    racing = asyncio.create_task(connected(("198.51.100.1", "203.0.113.1"), 80))

    await made.wait()
    await asyncio.sleep(0.05)  # the race has its winner, while the other attempt takes a second to end
    racing.cancel()
    try:
        winner, _address = await racing
    except asyncio.CancelledError:
        pass  # cancelled before it gave its winner, which then must not be left open
    else:
        winner.close()  # as the proxy does once done with it
    async with asyncio.timeout(5):
        while len(closed) < 2:
            await closing.wait()
            closing.clear()

    assert sorted(closed) == ["198.51.100.1", "203.0.113.1"]


async def test_hosts_that_cannot_be_reached_fail_as_curl_would(
    resolver: Callable[[Mapping[str, list[str]]], None],
) -> None:
    resolver({"localhost": ["127.0.0.1"]})
    with socket.create_server(("127.0.0.1", 0)) as listening:
        closed = listening.getsockname()[1]

    async with _proxy(AddressGuard(frozenset({"localhost"}))) as proxy:
        unresolved, _reader, writer = await _socks5(proxy, "nothing.example", 443)
        await _closed(writer)
        refused, _reader, writer = await _socks5(proxy, "localhost", closed)
        await _closed(writer)

    assert (unresolved, refused) == (HOST_UNREACHABLE, CONNECTION_REFUSED)
    failure = proxy.failure("https://nothing.example/")
    assert failure is not None
    assert (failure.kind, failure.code, failure.refused) == (ConnectError, 6, False)
    assert failure.reason.startswith("could not resolve nothing.example: ")
    failure = proxy.failure(f"http://localhost:{closed}/")
    assert failure is not None
    assert (failure.kind, failure.code) == (ConnectError, 7)
    assert failure.reason.startswith(f"could not connect to localhost port {closed}: ")


async def test_under_the_network_guard_only_loopback_hosts_are_reached(
    resolver: Callable[[Mapping[str, list[str]]], None],
) -> None:
    resolver({"localhost": ["127.0.0.1"]})  # intranet.example is never resolved
    guard = AddressGuard(frozenset({"intranet.example", "localhost"}))

    async with _echo() as (port, received), _proxy(guard, network_guard=NetworkGuard()) as proxy:
        refused, _reader, writer = await _socks5(proxy, "intranet.example", port)
        await _closed(writer)
        admitted, reader, writer = await _socks5(proxy, "localhost", port)
        writer.write(b"ping")
        assert await reader.read() == b"ping"
        await _closed(writer)

    assert (refused, admitted, received) == (NOT_ALLOWED, SUCCEEDED, [b"ping"])
    failure = proxy.failure(f"http://intranet.example:{port}/")
    assert failure is not None
    assert failure.reason == "the network guard admits only loopback hosts, such as localhost, not intranet.example"


async def test_clients_it_cannot_serve_are_told_so() -> None:
    async with _proxy(AddressGuard()) as proxy:
        no_method, _reader, writer = await _socks5(proxy, "example.com", 80, methods=b"\x02")  # credentials only
        await _closed(writer)
        bind, _reader, writer = await _socks5(proxy, "example.com", 80, command=2)
        await _closed(writer)

    assert (no_method, bind) == (0xFF, COMMAND_NOT_SUPPORTED)


async def test_closing_closes_the_connections_it_carries() -> None:
    async with _echo() as (port, _received):
        proxy = AddressGuardProxy(AddressGuard(frozenset({"localhost"})))
        await proxy.start()
        code, reader, writer = await _socks5(proxy, "localhost", port)  # the echo server waits for its first bytes

        async with asyncio.timeout(5):
            await proxy.close()
            assert await reader.read() == b""
        await _closed(writer)

    assert code == SUCCEEDED
    with pytest.raises(RuntimeError, match="not started"):
        _ = proxy.url


def test_failures_become_the_errors_of_the_hops_they_failed() -> None:
    refused = ProxiedFailure("127.0.0.1 is a loopback address", NetworkBlocked).error("http://127.0.0.1/")
    unresolved = ProxiedFailure("could not resolve a.example: no address", ConnectError, 6).error("http://a.example/")
    slow = ProxiedFailure("connecting to a.example port 80 took longer than 30 s", Timeout).error("http://a.example/")

    assert (type(refused), str(refused)) == (
        NetworkBlocked,
        "network.public_only refused http://127.0.0.1/: 127.0.0.1 is a loopback address",
    )
    assert (type(unresolved), str(unresolved), unresolved.code) == (
        ConnectError,
        "loading http://a.example/ failed: could not resolve a.example: no address",
        6,
    )
    assert type(slow) is Timeout


def test_launch_options_that_would_go_around_the_proxy_are_refused_while_only_public_hosts_may_be_reached() -> None:
    launch_options: dict[str, object] = {
        "proxy": {"server": "http://proxy.example:3128"},
        "args": ["--mute-audio", "--proxy-server=http://proxy.example:3128", "--webrtc-ip-handling-policy=default"],
        "ignore_default_args": True,
    }
    settings = BrowserSettings(launch_options=launch_options)

    with pytest.raises(SettingsError) as refused:
        BrowserFetcher(settings, network=NetworkSettings(public_only=True))

    assert [problem.split(" would take")[0] for problem in refused.value.problems] == [
        "browser.launch_options: 'proxy'",
        "browser.launch_options: 'ignore_default_args': true",
        "browser.launch_options: '--proxy-server=http://proxy.example:3128' in 'args'",
        "browser.launch_options: '--webrtc-ip-handling-policy=default' in 'args'",
    ]
    BrowserFetcher(settings)  # taken as they are while any host may be reached
    no_proxy_args = BrowserSettings(launch_options={"ignore_default_args": ["--mute-audio"], "args": ["--mute-audio"]})
    BrowserFetcher(no_proxy_args, network=NetworkSettings(public_only=True))
