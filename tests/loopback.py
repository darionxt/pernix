"""An in-process HTTP server on the loopback interface, for tests.

Routes are registered per test. Every request the server receives is recorded
with its headers in wire order, so tests can assert on exactly what was sent.
"""

from __future__ import annotations

import threading
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
