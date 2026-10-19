"""Accepting connections on 127.0.0.1 for the proxies a browser is launched with, closing every one with the listener."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["LoopbackListener"]

logger = logging.getLogger(__name__)


class LoopbackListener:
    """Listens on a port of 127.0.0.1 of its own, and serves each connection in a task of its own.

    It accepts connections itself, as the event loop tells it they wait, rather than
    through an `asyncio.Server`, which leaves unclosed a connection it accepted just
    before it was closed; a browser still running then connects to it all the same.
    Where the event loop cannot watch sockets so, as on Windows, a task accepts them,
    which may leave a connection accepted just as the listener closes to the garbage
    collector.
    """

    def __init__(self, serve: Callable[[socket.socket], Awaitable[None]]) -> None:
        """Create the listener; it listens once started.

        Args:
            serve: Serves a connection, a non-blocking socket, which is closed once
                it returns, or once the listener closes.
        """
        self._serve = serve
        self._listening: socket.socket | None = None
        self._watched = False
        self._accepting: asyncio.Task[None] | None = None
        self._connections: dict[socket.socket, asyncio.Task[None]] = {}

    @property
    def port(self) -> int:
        """The port it listens on.

        Raises:
            RuntimeError: It is not started.
        """
        if self._listening is None:
            raise RuntimeError("the listener is not started")
        return int(self._listening.getsockname()[1])

    async def start(self) -> None:
        """Start listening."""
        loop = asyncio.get_running_loop()
        listening = socket.create_server(("127.0.0.1", 0))
        listening.setblocking(False)
        self._listening = listening
        try:
            loop.add_reader(listening.fileno(), self._accept, listening)
        except NotImplementedError:  # an event loop that cannot watch sockets
            self._accepting = loop.create_task(self._accept_each(listening))
        else:
            self._watched = True

    async def close(self) -> None:
        """Stop listening, and close the connections still open, as those of a browser that went away are."""
        listening, self._listening = self._listening, None
        if listening is None:
            return
        if self._watched:
            asyncio.get_running_loop().remove_reader(listening.fileno())
            self._watched = False
        accepting, self._accepting = self._accepting, None
        if accepting is not None:
            accepting.cancel()
            await asyncio.wait([accepting])
        listening.close()
        serving = list(self._connections.values())
        for task in serving:
            task.cancel()
        if serving:
            await asyncio.wait(serving)
        for connection in list(self._connections):  # those whose task was cancelled before it ran
            connection.close()
        self._connections.clear()

    def _accept(self, listening: socket.socket) -> None:
        """Take the connections waiting, each into a task of its own."""
        while True:
            try:
                connection, _address = listening.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError:  # out of descriptors, say: the next readiness tries again
                return
            self._served(connection)

    async def _accept_each(self, listening: socket.socket) -> None:
        """Take each connection as it arrives, for an event loop that cannot watch sockets."""
        loop = asyncio.get_running_loop()
        while True:
            connection, _address = await loop.sock_accept(listening)
            self._served(connection)

    def _served(self, connection: socket.socket) -> None:
        connection.setblocking(False)
        self._connections[connection] = asyncio.get_running_loop().create_task(self._serving(connection))

    async def _serving(self, connection: socket.socket) -> None:
        try:
            await self._serve(connection)
        except OSError:
            pass  # the client went away
        except Exception:
            logger.warning("Serving a connection on 127.0.0.1 failed", exc_info=True)
        finally:
            self._connections.pop(connection, None)
            with contextlib.suppress(OSError):
                connection.close()
