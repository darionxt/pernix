from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from pernix.fetchers.loopback_listener import LoopbackListener

if TYPE_CHECKING:
    import socket

__all__ = ["RefusingProxy"]

_HEAD_TIMEOUT = 5.0
"""Seconds to wait for the head of a request before closing the connection anyway."""

_HEAD_LIMIT = 65536
"""Bytes of a request head read at most before closing the connection anyway."""


class RefusingProxy:
    """A proxy on 127.0.0.1 that answers nothing: it closes every connection once the request arrived.

    A browser launched under the network guard sends everything but loopback traffic
    to it, its own requests that belong to no page included, so none of it reaches
    the network.
    """

    def __init__(self) -> None:
        """Create the proxy; it listens once started."""
        self._listener = LoopbackListener(_refused)

    @property
    def url(self) -> str:
        """The URL of the proxy, such as `http://127.0.0.1:49152`.

        Raises:
            RuntimeError: The proxy is not started.
        """
        try:
            port = self._listener.port
        except RuntimeError:
            raise RuntimeError("the refusing proxy is not started") from None
        return f"http://127.0.0.1:{port}"

    async def start(self) -> None:
        """Start listening on a port of its own."""
        await self._listener.start()

    async def close(self) -> None:
        """Stop listening, and close the connections still open, as those of a browser that went away are."""
        await self._listener.close()


async def _refused(connection: socket.socket) -> None:
    """Return once a connection's request head arrived, so the client sees it closed rather than reset."""
    loop = asyncio.get_running_loop()
    head = b""
    try:
        async with asyncio.timeout(_HEAD_TIMEOUT):
            while b"\r\n\r\n" not in head and len(head) < _HEAD_LIMIT:
                received = await loop.sock_recv(connection, 4096)
                if not received:
                    break
                head += received
    except TimeoutError:
        pass  # closed all the same
