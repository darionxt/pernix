"""The proxy that refuses what a guarded browser must not reach."""

from __future__ import annotations

import asyncio
import gc
import socket
from urllib.parse import urlsplit

from pernix.fetchers.refusing_proxy import RefusingProxy


def _address(proxy: RefusingProxy) -> tuple[str, int]:
    parts = urlsplit(proxy.url)
    assert parts.hostname is not None
    assert parts.port is not None
    return parts.hostname, parts.port


async def test_a_request_is_answered_by_closing_its_connection() -> None:
    proxy = RefusingProxy()
    await proxy.start()
    try:
        reader, writer = await asyncio.open_connection(*_address(proxy))
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
        async with asyncio.timeout(5):
            assert await reader.read() == b""  # closed, with nothing said
        writer.close()
        await writer.wait_closed()
    finally:
        await proxy.close()


async def test_closing_closes_the_connections_still_open() -> None:
    proxy = RefusingProxy()
    await proxy.start()
    reader, writer = await asyncio.open_connection(*_address(proxy))
    await asyncio.sleep(0.05)  # accepted, waiting for a request head

    async with asyncio.timeout(1):  # well before the head would time out
        await proxy.close()
        assert await reader.read() == b""
    writer.close()
    await writer.wait_closed()


async def test_connections_arriving_as_it_closes_are_never_left_open() -> None:
    proxy = RefusingProxy()
    await proxy.start()
    clients = [socket.create_connection(_address(proxy)) for _ in range(20)]
    for _ in range(3):
        await asyncio.sleep(0)  # some accepted, some not yet

    await proxy.close()
    for client in clients:
        client.close()
    gc.collect()  # a connection left open would warn, failing the test
