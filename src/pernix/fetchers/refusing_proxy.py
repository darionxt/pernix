"""A proxy that refuses whatever a browser sends it: the browser's own requests, and, under the network guard, more."""

from __future__ import annotations

import asyncio
import collections
import logging
from typing import TYPE_CHECKING

from pernix.fetchers.loopback_listener import LoopbackListener

if TYPE_CHECKING:
    import socket

__all__ = ["RefusingProxy"]

logger = logging.getLogger(__name__)

_HEAD_TIMEOUT = 5.0
"""Seconds to wait for the head of a request before closing the connection anyway."""

_HEAD_LIMIT = 65536
"""Bytes of a request head read at most before closing the connection anyway."""

_KEPT = 256
"""The targets refused that `refused` keeps, the latest."""


class RefusingProxy:
    """A proxy on 127.0.0.1 that answers nothing: it closes every connection once the request arrived.

    Every browser is launched with it, so that its own requests, which belong to no
    context and so ignore the proxies of its contexts, reach no network; under the
    network guard, a browser sends it everything but loopback traffic too.

    Attributes:
        refused: The targets of the latest requests refused, such as
            `accounts.google.com:443` for a `CONNECT`, or the URL of a plain HTTP
            request, without its query, oldest first.
    """

    def __init__(self) -> None:
        """Create the proxy; it listens once started."""
        self._listener = LoopbackListener(self._refuse)
        self.refused: collections.deque[str] = collections.deque(maxlen=_KEPT)

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

    async def _refuse(self, connection: socket.socket) -> None:
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
        target = _target(head)
        if target is not None:
            self.refused.append(target)
            logger.debug("The refusing proxy refused %s", target)


def _target(head: bytes) -> str | None:
    """What a request asks a proxy for: the host and port of a `CONNECT`, or a URL without its query."""
    line = head.split(b"\r\n", 1)[0].decode("latin-1")
    parts = line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/"):
        return None
    return parts[1].split("?", 1)[0]
