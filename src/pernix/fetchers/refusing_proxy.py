from __future__ import annotations

import asyncio

__all__ = ["RefusingProxy"]

_HEAD_TIMEOUT = 5.0
"""Seconds to wait for the head of a request before closing the connection anyway."""


class RefusingProxy:
    """A proxy on 127.0.0.1 that answers nothing: it closes every connection once the request arrived.

    A browser launched under the network guard sends everything but loopback traffic
    to it, its own requests that belong to no page included, so none of it reaches
    the network.
    """

    def __init__(self) -> None:
        """Create the proxy; it listens once started."""
        self._server: asyncio.Server | None = None

    @property
    def url(self) -> str:
        """The URL of the proxy, such as `http://127.0.0.1:49152`.

        Raises:
            RuntimeError: The proxy is not started.
        """
        if self._server is None:
            raise RuntimeError("the refusing proxy is not started")
        host, port = self._server.sockets[0].getsockname()[:2]
        return f"http://{host}:{port}"

    async def start(self) -> None:
        """Start listening on a port of its own."""
        self._server = await asyncio.start_server(_refuse, "127.0.0.1", 0)

    async def close(self) -> None:
        """Stop listening, and close the connections still open, as those of a browser that went away are."""
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.close_clients()
            await server.wait_closed()


async def _refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Close a connection once its request head arrived, so the client sees it end rather than reset."""
    try:
        async with asyncio.timeout(_HEAD_TIMEOUT):
            await reader.readuntil(b"\r\n\r\n")
    except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass  # closed all the same
    finally:
        writer.close()
