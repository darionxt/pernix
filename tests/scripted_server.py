"""A raw TCP server on the loopback interface, for HTTP edge cases an HTTP server library will not produce."""

from __future__ import annotations

import socket
import threading
import time
from typing import Self


class ScriptedServer:
    """A TCP server on the loopback interface that answers each connection with a fixed script.

    After reading the request head, it sends each `(data, pause)` step after sleeping
    `pause` seconds, then closes the connection. Tests use it for what an HTTP server
    library will not do: stall, trickle, or leave out `Content-Length`. `completed`
    records, for each connection answered, whether every step was sent before the
    client went away, and `answered` is set after each.
    """

    def __init__(self, steps: list[tuple[bytes, float]]) -> None:
        self._steps = steps
        self.completed: list[bool] = []
        self.answered = threading.Event()
        self._socket = socket.create_server(("127.0.0.1", 0))
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def url(self) -> str:
        host, port = self._socket.getsockname()[:2]
        return f"http://{host}:{port}/"

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._socket.close()

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._answer, args=(connection,), daemon=True).start()

    def _answer(self, connection: socket.socket) -> None:
        with connection:
            head = b""
            while b"\r\n\r\n" not in head:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                head += chunk
            completed = False
            try:
                for data, pause in self._steps:
                    time.sleep(pause)
                    connection.sendall(data)
                completed = True
            except OSError:
                pass  # the client gave up
            finally:
                self.completed.append(completed)
                self.answered.set()
