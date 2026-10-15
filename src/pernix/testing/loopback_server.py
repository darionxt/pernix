from __future__ import annotations

import mimetypes
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import TYPE_CHECKING, Self, override
from urllib.parse import parse_qsl, unquote, urlsplit

if TYPE_CHECKING:
    import os
    from collections.abc import Mapping

__all__ = ["Handler", "LoopbackServer", "ReceivedRequest", "Reply"]


@dataclass(frozen=True)
class ReceivedRequest:
    """A request the server received.

    Attributes:
        method: The HTTP method.
        path: The path, without the query.
        query: The query parameters, in order, blank values kept.
        headers: The headers, in wire order.
        body: The body.
    """

    method: str
    path: str
    query: list[tuple[str, str]]
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        """Return the first value of a header, matched case-insensitively.

        Args:
            name: The header name.

        Returns:
            Its first value, or `None` if the request has no such header.
        """
        lowered = name.lower()
        return next((v for k, v in self.headers if k.lower() == lowered), None)


@dataclass(frozen=True)
class Reply:
    """A response the server sends.

    Attributes:
        body: The body; a `str` is sent as UTF-8.
        status: The status code.
        headers: The headers, in order; `Content-Length` is added unless given.
    """

    body: bytes | str = b""
    status: int = 200
    headers: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def encoded_body(self) -> bytes:
        """Return the body as bytes."""
        return self.body.encode() if isinstance(self.body, str) else self.body


type Handler = Callable[[ReceivedRequest], Reply]
"""Answers a request; it runs in a thread of the server."""


class LoopbackServer:
    """A threaded HTTP/1.1 server on 127.0.0.1, on a port of its own, for tests.

    It answers the routes registered with `route()` or given as `routes`, then the
    files under `directory`, if given, and 404 otherwise. Every request it receives
    is recorded with its headers in wire order, so tests can check exactly what was
    sent. Use it as a context manager, or through the `loopback_server` fixture.

    Example:
        >>> with LoopbackServer(routes={"/": "<a href='/2'>next</a>", "/2": "<p>end</p>"}) as server:
        ...     Pages.run(start=server.url("/"))
    """

    def __init__(
        self,
        routes: Mapping[str, str | bytes | Reply | Handler] | None = None,
        *,
        directory: str | os.PathLike[str] | None = None,
    ) -> None:
        """Create the server; it starts serving once entered, or with `start()`.

        Args:
            routes: GET routes by path: a body to send with status 200, a `Reply`,
                or a `Handler`.
            directory: Serve the files under it to GET and HEAD requests no route
                answers, with a `Content-Type` guessed from the extension; a
                directory serves its `index.html`.
        """
        self._routes: dict[tuple[str, str], Handler] = {}
        self._received: list[ReceivedRequest] = []
        self._lock = threading.Lock()
        self._directory = None if directory is None else Path(directory).resolve()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        for path, answer in (routes or {}).items():
            reply = Reply(answer) if isinstance(answer, str | bytes) else answer
            self.route(path, handler=_fixed(reply) if isinstance(reply, Reply) else reply)

    @property
    def base_url(self) -> str:
        """The URL of the server, such as `http://127.0.0.1:49152`, without a trailing slash."""
        host, port = self._httpd.server_address[:2]
        return f"http://{host!s}:{port}"

    def url(self, path: str = "/") -> str:
        """Return the URL of a path on the server.

        Args:
            path: The path, with its query if any.

        Returns:
            The absolute URL.
        """
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
        """Answer a method and path with a fixed reply, or with a handler.

        Args:
            path: The path, without the query.
            body: The body of the fixed reply.
            status: The status of the fixed reply.
            headers: The headers of the fixed reply.
            method: The HTTP method to answer.
            handler: Answers instead of the fixed reply.
        """
        answer = handler or _fixed(Reply(body=body, status=status, headers=headers or []))
        with self._lock:
            self._routes[method.upper(), path] = answer

    @property
    def received(self) -> list[ReceivedRequest]:
        """The requests received so far, in order."""
        with self._lock:
            return list(self._received)

    def start(self) -> None:
        """Start serving in a thread."""
        self._thread.start()

    def stop(self) -> None:
        """Stop serving and close the socket."""
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
        if handler is not None:
            return handler(request)
        if request.method in ("GET", "HEAD") and (reply := self._file(request.path)) is not None:
            return reply
        return Reply(body=b"not found", status=404)

    def _file(self, path: str) -> Reply | None:
        """The file a path names under the directory, if any."""
        if self._directory is None:
            return None
        target = (self._directory / unquote(path).lstrip("/")).resolve()
        if target.is_dir():
            target = (target / "index.html").resolve()
        if not target.is_relative_to(self._directory):
            return None  # outside the directory, as with "/../secret" or a link out of it
        if not target.is_file():
            return None
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return Reply(body=target.read_bytes(), headers=[("Content-Type", content_type)])

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


def _fixed(reply: Reply) -> Handler:
    def fixed(_request: ReceivedRequest) -> Reply:
        return reply

    return fixed
