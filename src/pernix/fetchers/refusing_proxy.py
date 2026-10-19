from __future__ import annotations

import asyncio
import contextlib
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

    It accepts connections itself, as the event loop tells it they wait, rather than
    through an `asyncio.Server`, which leaves unclosed a connection it accepted just
    before it was closed; a browser still running then connects to it all the same.
    Where the event loop cannot watch sockets so, as on Windows, it uses one.
    """

    def __init__(self) -> None:
        """Create the proxy; it listens once started."""
        self._listening: socket.socket | None = None
        self._server: asyncio.Server | None = None
        self._connections: dict[socket.socket, asyncio.Task[None]] = {}

    @property
    def url(self) -> str:
        """The URL of the proxy, such as `http://127.0.0.1:49152`.

        Raises:
            RuntimeError: The proxy is not started.
        """
        if self._listening is not None:
            host, port = self._listening.getsockname()[:2]
        elif self._server is not None:
            host, port = self._server.sockets[0].getsockname()[:2]
        else:
            raise RuntimeError("the refusing proxy is not started")
        return f"http://{host}:{port}"

    async def start(self) -> None:
        """Start listening on a port of its own."""
        loop = asyncio.get_running_loop()
        listening = socket.create_server(("127.0.0.1", 0))
        listening.setblocking(False)
        try:
            loop.add_reader(listening.fileno(), self._accept, listening)
        except NotImplementedError:  # an event loop that cannot watch sockets
            listening.close()
            self._server = await asyncio.start_server(_refuse, "127.0.0.1", 0)
            return
        self._listening = listening

    async def close(self) -> None:
        """Stop listening, and close the connections still open, as those of a browser that went away are."""
        listening, self._listening = self._listening, None
        server, self._server = self._server, None
        if server is not None:
            server.close()
            server.close_clients()
            await server.wait_closed()
        if listening is None:
            return
        asyncio.get_running_loop().remove_reader(listening.fileno())
        listening.close()
        refusing = list(self._connections.values())
        for task in refusing:
            task.cancel()
        if refusing:
            await asyncio.wait(refusing)
        for connection in list(self._connections):  # those whose task was cancelled before it ran
            connection.close()
        self._connections.clear()

    def _accept(self, listening: socket.socket) -> None:
        """Take the connections waiting, each into a task of its own that refuses it."""
        loop = asyncio.get_running_loop()
        while True:
            try:
                connection, _address = listening.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:  # out of descriptors, say: the next readiness tries again
                return
            connection.setblocking(False)
            self._connections[connection] = loop.create_task(self._refused(connection))

    async def _refused(self, connection: socket.socket) -> None:
        """Close a connection once its request head arrived, so the client sees it end rather than reset."""
        loop = asyncio.get_running_loop()
        head = b""
        try:
            async with asyncio.timeout(_HEAD_TIMEOUT):
                while b"\r\n\r\n" not in head and len(head) < _HEAD_LIMIT:
                    received = await loop.sock_recv(connection, 4096)
                    if not received:
                        break
                    head += received
        except (TimeoutError, OSError):
            pass  # closed all the same
        finally:
            self._connections.pop(connection, None)
            connection.close()


async def _refuse(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Close a connection once its request head arrived, for an event loop that serves it."""
    try:
        async with asyncio.timeout(_HEAD_TIMEOUT):
            await reader.readuntil(b"\r\n\r\n")
    except (TimeoutError, OSError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        pass  # closed all the same
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
