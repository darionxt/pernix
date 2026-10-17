"""A loopback server that records the cookie fields of each request, over HTTP/1.1 and HTTP/2."""

from __future__ import annotations

import socket
import ssl
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import h2.exceptions

if TYPE_CHECKING:
    from collections.abc import Iterable

    import trustme

HOSTS = ("127.0.0.1", "::1", "localhost", "app.localhost", "intranet")
"""The names the server's certificate is valid for."""

_TLS_HANDSHAKE = 0x16
"""The first byte of a TLS connection."""


@dataclass(frozen=True)
class Received:
    """A request the server received.

    Attributes:
        protocol: `HTTP/1.1` or `HTTP/2`.
        host: The `Host` header, or the `:authority` pseudo-header.
        path: The path, without the query.
        cookies: Each `Cookie` header (HTTP/1.1), or each `cookie` field (HTTP/2), in wire order.
        peer: The client's port, which tells connections apart.
        user_agent: The `User-Agent` header.
    """

    protocol: str
    host: str
    path: str
    cookies: list[str]
    peer: int = 0
    user_agent: str = ""


@dataclass
class CookieServer:
    """A threaded server on one loopback port, speaking HTTP and HTTPS alike.

    A connection that starts with a TLS handshake is HTTPS, with HTTP/2 or HTTP/1.1
    as ALPN chooses; any other is HTTP/1.1, where a request in absolute form is
    answered as if the server were the host it names, and `CONNECT` opens a tunnel to
    the server's HTTPS side. So the server is also the proxy for names that do not
    resolve, such as `intranet`.

    Attributes:
        address: The loopback address to listen on.
        routes: Response headers by path; a `Location` header makes the response a 302.
        early_hints: The headers of a 103 Early Hints response sent first, by path,
            over HTTP/1.1.
        connect_headers: The headers of the answer to `CONNECT`.
        received: The requests received, in order.
    """

    ca: trustme.CA
    address: str = "127.0.0.1"
    routes: dict[str, list[tuple[str, str]]] = field(default_factory=dict[str, list[tuple[str, str]]])
    early_hints: dict[str, list[tuple[str, str]]] = field(default_factory=dict[str, list[tuple[str, str]]])
    connect_headers: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])
    received: list[Received] = field(default_factory=list[Received])

    def __post_init__(self) -> None:
        family = socket.AF_INET6 if ":" in self.address else socket.AF_INET
        self._socket = socket.create_server((self.address, 0), family=family)
        self._tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        self.ca.issue_cert(*HOSTS).configure_cert(self._tls)
        self._tls.set_alpn_protocols(["h2", "http/1.1"])

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    @property
    def proxy(self) -> str:
        """The server as a proxy URL."""
        return f"http://{self._authority(self.address)}"

    def url(self, path: str = "/", *, host: str | None = None, scheme: str = "http") -> str:
        """Return a URL on this server.

        Args:
            path: The path.
            host: The host name in the URL; the address listened on by default.
            scheme: `http` or `https`.

        Returns:
            The URL, with the server's port.
        """
        return f"{scheme}://{self._authority(host or self.address)}{path}"

    def __enter__(self) -> Self:
        threading.Thread(target=self._serve, daemon=True).start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._socket.close()

    def _authority(self, host: str) -> str:
        return f"[{host}]:{self.port}" if ":" in host else f"{host}:{self.port}"

    def _serve(self) -> None:
        while True:
            try:
                connection, _ = self._socket.accept()
            except OSError:
                return  # closed
            threading.Thread(target=self._answer, args=(connection,), daemon=True).start()

    def _answer(self, connection: socket.socket) -> None:
        try:
            with connection:
                peer = connection.getpeername()[1]
                first = connection.recv(1, socket.MSG_PEEK)
                if first and first[0] == _TLS_HANDSHAKE:
                    self._answer_tls(connection, peer)
                elif first:
                    self._answer_http(connection, peer, tunnel=True)
        except (OSError, h2.exceptions.ProtocolError):
            return  # the client went away

    def _answer_tls(self, connection: socket.socket, peer: int) -> None:
        with self._tls.wrap_socket(connection, server_side=True) as tls:
            if tls.selected_alpn_protocol() == "h2":
                self._answer_h2(tls, peer)
            else:
                self._answer_http(tls, peer, tunnel=False)

    def _answer_http(self, connection: socket.socket, peer: int, *, tunnel: bool) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = connection.recv(65536)
            if not chunk:
                return
            head += chunk
        request_line, *lines = head.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
        method, target, _ = request_line.split(" ")
        if tunnel and method == "CONNECT":
            fields = "".join(f"{name}: {value}\r\n" for name, value in self.connect_headers)
            connection.sendall(f"HTTP/1.1 200 Connection established\r\n{fields}\r\n".encode())
            self._answer_tls(connection, peer)
            return
        headers = [(name.strip(), value.strip()) for name, _, value in (line.partition(":") for line in lines)]
        fields = {name.lower(): value for name, value in headers}
        cookies = [value for name, value in headers if name.lower() == "cookie"]
        self.received.append(
            Received(
                "HTTP/1.1", fields.get("host", ""), urlsplit(target).path, cookies, peer, fields.get("user-agent", "")
            )
        )
        path = urlsplit(target).path
        if hints := self.early_hints.get(path):
            fields = "".join(f"{name}: {value}\r\n" for name, value in hints)
            connection.sendall(f"HTTP/1.1 103 Early Hints\r\n{fields}\r\n".encode())
        reply = self.routes.get(path, [])
        status = "302 Found" if any(name.lower() == "location" for name, _ in reply) else "200 OK"
        fields = "".join(f"{name}: {value}\r\n" for name, value in reply)
        connection.sendall(f"HTTP/1.1 {status}\r\n{fields}Content-Length: 0\r\nConnection: close\r\n\r\n".encode())

    def _answer_h2(self, connection: ssl.SSLSocket, peer: int) -> None:
        config = h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8", normalize_inbound_headers=False
        )  # keeps each cookie field apart
        h2_connection = h2.connection.H2Connection(config)
        h2_connection.initiate_connection()
        connection.sendall(h2_connection.data_to_send())
        while data := connection.recv(65536):
            for event in h2_connection.receive_data(data):
                if isinstance(event, h2.events.RequestReceived) and event.stream_id is not None:
                    headers = [(str(name), str(value)) for name, value in event.headers or []]
                    self._answer_stream(h2_connection, event.stream_id, headers, peer)
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return
            connection.sendall(h2_connection.data_to_send())

    def _answer_stream(
        self, connection: h2.connection.H2Connection, stream_id: int, headers: Iterable[tuple[str, str]], peer: int
    ) -> None:
        pairs = list(headers)
        fields = dict(pairs)
        path = urlsplit(fields.get(":path", "")).path
        cookies = [value for name, value in pairs if name == "cookie"]
        self.received.append(
            Received("HTTP/2", fields.get(":authority", ""), path, cookies, peer, fields.get("user-agent", ""))
        )
        reply = [(name.lower(), value) for name, value in self.routes.get(path, [])]
        status = "302" if any(name == "location" for name, _ in reply) else "200"
        connection.send_headers(stream_id, [(":status", status), *reply, ("content-length", "0")], end_stream=True)
