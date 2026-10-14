"""An in-process HTTP server on the loopback interface, for tests.

Routes are registered per test. Every request the server receives is recorded
with its headers in wire order, so tests can assert on exactly what was sent.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self, override
from urllib.parse import parse_qsl, urlsplit


@dataclass(frozen=True)
class ReceivedRequest:
    method: str
    path: str
    query: list[tuple[str, str]]
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        """Return the first value of a header, matched case-insensitively."""
        lowered = name.lower()
        return next((v for k, v in self.headers if k.lower() == lowered), None)


@dataclass(frozen=True)
class Reply:
    body: bytes | str = b""
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def encoded_body(self) -> bytes:
        return self.body.encode() if isinstance(self.body, str) else self.body


Handler = Callable[[ReceivedRequest], Reply]


class LoopbackServer:
    """A threaded HTTP/1.1 server bound to 127.0.0.1 on an ephemeral port."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], Handler] = {}
        self._received: list[ReceivedRequest] = []
        self._lock = threading.Lock()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def base_url(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str = "/") -> str:
        return self.base_url + path

    def route(
        self,
        path: str,
        body: bytes | str = b"",
        *,
        status: int = 200,
        headers: list[tuple[str, str]] | None = None,
        method: str = "GET",
        handler: Handler | None = None,
    ) -> None:
        """Serve a fixed reply at `path`, or delegate to `handler` when given."""
        if handler is None:
            reply = Reply(body=body, status=status, headers=headers or [])

            def fixed(_request: ReceivedRequest) -> Reply:
                return reply

            handler = fixed

        with self._lock:
            self._routes[method.upper(), path] = handler

    @property
    def received(self) -> list[ReceivedRequest]:
        with self._lock:
            return list(self._received)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def _dispatch(self, request: ReceivedRequest) -> Reply:
        with self._lock:
            self._received.append(request)
            handler = self._routes.get((request.method, request.path))
        if handler is None:
            return Reply(body=b"not found", status=404)
        return handler(request)

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                parts = urlsplit(self.path)
                request = ReceivedRequest(
                    method=self.command,
                    path=parts.path,
                    query=parse_qsl(parts.query, keep_blank_values=True),
                    headers=list(self.headers.items()),
                    body=body,
                )
                reply = server._dispatch(request)  # pyright: ignore[reportPrivateUsage]
                try:
                    self._reply(reply)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True  # the client gave up waiting, as timeout tests make it

            def _reply(self, reply: Reply) -> None:
                payload = reply.encoded_body()
                self.send_response(reply.status)
                for name, value in reply.headers:
                    self.send_header(name, value)
                if not any(name.lower() == "content-length" for name, _ in reply.headers):
                    self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

            @override
            def log_message(self, format: str, *args: object) -> None:
                pass

        return _Handler


class ScriptedServer:
    """A TCP server on the loopback interface that answers each connection with a fixed script.

    After reading the request head, it sends each `(data, pause)` step after sleeping
    `pause` seconds, then closes the connection. Tests use it for what an HTTP server
    library will not do: stall, trickle, or leave out `Content-Length`.
    """

    def __init__(self, steps: list[tuple[bytes, float]]) -> None:
        self._steps = steps
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
            try:
                for data, pause in self._steps:
                    time.sleep(pause)
                    connection.sendall(data)
            except OSError:
                pass  # the client gave up
