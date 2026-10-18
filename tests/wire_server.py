"""A loopback server that records, per connection, what a client put on the wire: TLS, HTTP/2, and HTTP/1.1."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import itertools
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self
from urllib.parse import urlsplit

import h2.config
import h2.connection
import h2.events
import h2.exceptions
import h2.settings
from cryptography import x509
from cryptography.hazmat.primitives import serialization

from wire_capture import (
    TLS_HANDSHAKE,
    ClientHello,
    Frame,
    HeaderBlock,
    Http2Reader,
    client_hello_size,
    parse_client_hello,
)

if TYPE_CHECKING:
    import trustme

HOST = "localhost"
"""The host name clients reach the server by, so that their ClientHello names it."""

PAGE = b'<!doctype html><html><head><link rel="icon" href="data:,"></head><body><p>page</p></body></html>'
"""The page every request gets; its empty icon keeps the browser from asking for `/favicon.ico`."""

_ANNOUNCED = (h2.settings.SettingCodes.MAX_CONCURRENT_STREAMS, h2.settings.SettingCodes.MAX_HEADER_LIST_SIZE)
"""The settings the server announces: those of h2 that are not the protocol's defaults, as many servers announce only those."""

_ACCEPT_POLL = 0.05
"""Seconds the server waits for a connection before checking whether it is stopped."""

_PEEK_TIMEOUT = 5.0
"""Seconds a client may take to send its whole ClientHello."""

_CLOSE_TIMEOUT = 5.0
"""Seconds a connection the server closes may take to close."""

_LINGER = 2.0
"""Seconds the server waits, after sending GOAWAY, for the client to close the connection first."""


@dataclass(frozen=True)
class CapturedRequest:
    """A request as the client sent it.

    Attributes:
        sequence: Its place among every request the server received.
        path: The path, without the query.
        headers: The header fields in wire order: over HTTP/2, the pseudo-headers
            first, each `cookie` field apart; over HTTP/1.1, with the case of
            their names as sent.
        block: Its HEADERS frame and the HPACK representations of its fields, over
            HTTP/2.
        request_line: Its request line, over HTTP/1.1.
    """

    sequence: int
    path: str
    headers: list[tuple[str, str]]
    block: HeaderBlock | None = None
    request_line: str | None = None


@dataclass
class CapturedConnection:
    """What a client sent on one connection.

    Attributes:
        protocol: `HTTP/2` or `HTTP/1.1`.
        client_hello: The ClientHello, unless the connection is cleartext.
        frames: The HTTP/2 frames, in order.
        requests: The requests, in order.
        closed: Set once the connection is closed.
    """

    protocol: str = "HTTP/1.1"
    client_hello: ClientHello | None = None
    frames: list[Frame] = field(default_factory=list[Frame])
    requests: list[CapturedRequest] = field(default_factory=list[CapturedRequest])
    closed: threading.Event = field(default_factory=threading.Event)

    @property
    def resumed(self) -> bool:
        """Whether the client offered to resume a TLS session, with a pre-shared key."""
        return self.client_hello is not None and self.client_hello.extension("pre_shared_key") is not None


class WireServer:
    """A threaded server on one loopback port, recording what each connection carried.

    A connection that starts with a TLS handshake is HTTPS: its ClientHello is
    recorded before the handshake goes on, then it speaks HTTP/2 or HTTP/1.1 as
    ALPN chooses. Any other connection is HTTP/1.1. Over HTTP/2, each frame the
    client sends is recorded, with the HPACK representations of each header block.
    Every request gets the same small page, uncached; after answering one for a
    path in `closing`, the server closes the connection, with a GOAWAY over HTTP/2,
    so the client connects again for its next request, resuming the TLS session it
    was given a ticket for.

    The certificate comes from a CA curl can be made to trust; a browser trusts it
    through `spki`.

    Attributes:
        closing: The paths after which the server closes the connection.
    """

    def __init__(
        self,
        ca: trustme.CA,
        *,
        protocols: tuple[str, ...] = ("h2", "http/1.1"),
        announces_header_table_size: bool = False,
    ) -> None:
        """Create the server; it starts once entered.

        Args:
            ca: The certificate authority its certificate comes from.
            protocols: The protocols it offers through ALPN, preferred first.
            announces_header_table_size: Also announce `SETTINGS_HEADER_TABLE_SIZE`,
                4096, the size the protocol starts with.
        """
        self.closing: set[str] = set()
        self._announces_header_table_size = announces_header_table_size
        self._socket = socket.create_server(("127.0.0.1", 0))
        leaf = ca.issue_cert(HOST, "127.0.0.1", "::1")
        self._tls = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
        leaf.configure_cert(self._tls)
        self._tls.set_alpn_protocols(list(protocols))
        self._spki = _spki_hash(leaf.cert_chain_pems[0].bytes())
        self._connections: list[CapturedConnection] = []
        self._sequence = itertools.count()
        self._lock = threading.Lock()
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)

    @property
    def port(self) -> int:
        return self._socket.getsockname()[1]

    @property
    def spki(self) -> str:
        """The SHA-256 hash of its certificate's public key, in base64, as Chrome's `--ignore-certificate-errors-spki-list` takes it."""
        return self._spki

    def url(self, path: str = "/", *, scheme: str = "https") -> str:
        """Return the URL of a path on the server, under its host name.

        Args:
            path: The path.
            scheme: `https`, or `http` for cleartext HTTP/1.1.

        Returns:
            The URL.
        """
        return f"{scheme}://{HOST}:{self.port}{path}"

    @property
    def connections(self) -> list[CapturedConnection]:
        """The connections accepted so far, in order."""
        with self._lock:
            return list(self._connections)

    async def closed_after(self, path: str) -> None:
        """Wait until the connection that carried the last request for a path is closed.

        Args:
            path: The path.

        Raises:
            AssertionError: No connection carried such a request, or it is still
                open after a few seconds.
        """
        carrying = [connection for connection in self.connections if any(r.path == path for r in connection.requests)]
        assert carrying, f"no request for {path} reached the server"
        closed = await asyncio.to_thread(carrying[-1].closed.wait, _CLOSE_TIMEOUT)
        assert closed, f"the connection of {path} is still open"

    def __enter__(self) -> Self:
        self._socket.settimeout(_ACCEPT_POLL)  # closing the socket would not wake an accept() on Linux
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stopped.set()
        self._thread.join(timeout=5)
        self._socket.close()

    def _next(self) -> int:
        """The place of the next request the server receives."""
        with self._lock:
            return next(self._sequence)

    def _serve(self) -> None:
        while not self._stopped.is_set():
            try:
                connection, _ = self._socket.accept()
            except TimeoutError:
                continue
            except OSError:
                return  # closed
            connection.settimeout(None)
            captured = CapturedConnection()
            with self._lock:
                self._connections.append(captured)
            threading.Thread(target=self._answer, args=(connection, captured), daemon=True).start()

    def _answer(self, connection: socket.socket, captured: CapturedConnection) -> None:
        try:
            with connection:
                first = connection.recv(1, socket.MSG_PEEK)
                if first and first[0] == TLS_HANDSHAKE:
                    self._answer_tls(connection, captured)
                elif first:
                    self._answer_http1(connection, captured)
        except (OSError, h2.exceptions.ProtocolError):
            pass  # the client went away
        finally:
            captured.closed.set()

    def _answer_tls(self, connection: socket.socket, captured: CapturedConnection) -> None:
        captured.client_hello = parse_client_hello(_peek_client_hello(connection))
        with self._tls.wrap_socket(connection, server_side=True) as tls:
            if tls.selected_alpn_protocol() == "h2":
                captured.protocol = "HTTP/2"
                self._answer_h2(tls, captured)
            else:
                self._answer_http1(tls, captured)

    def _answer_http1(self, connection: socket.socket, captured: CapturedConnection) -> None:
        buffered = b""
        while True:
            while b"\r\n\r\n" not in buffered:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffered += chunk
            head, _, buffered = buffered.partition(b"\r\n\r\n")
            request_line, *lines = head.decode("latin-1").split("\r\n")
            headers = [(name, value.strip()) for name, _, value in (line.partition(":") for line in lines)]
            length = int(next((value for name, value in headers if name.lower() == "content-length"), "0"))
            while len(buffered) < length:
                chunk = connection.recv(65536)
                if not chunk:
                    return
                buffered += chunk
            buffered = buffered[length:]
            path = urlsplit(request_line.split(" ")[1]).path
            captured.requests.append(CapturedRequest(self._next(), path, headers, request_line=request_line))
            closes = path in self.closing
            fields = "Content-Type: text/html\r\nCache-Control: no-store\r\n"
            fields += f"Content-Length: {len(PAGE)}\r\n{'Connection: close' if closes else 'Keep-Alive: timeout=5'}\r\n"
            connection.sendall(f"HTTP/1.1 200 OK\r\n{fields}\r\n".encode() + PAGE)
            if closes:
                return

    def _answer_h2(self, connection: ssl.SSLSocket, captured: CapturedConnection) -> None:
        config = h2.config.H2Configuration(
            client_side=False, header_encoding="utf-8", normalize_inbound_headers=False, validate_inbound_headers=False
        )  # keeps each cookie field apart, and the fields as sent
        h2_connection = h2.connection.H2Connection(config)
        h2_connection.initiate_connection()
        h2_connection.data_to_send()  # announced instead by the server's own SETTINGS frame
        connection.sendall(self._settings_frame(h2_connection))
        reader = Http2Reader()
        captured.frames = reader.frames
        closing = False
        while data := connection.recv(65536):
            reader.feed(data)
            for event in h2_connection.receive_data(data):
                if isinstance(event, h2.events.RequestReceived) and event.stream_id is not None:
                    headers = [(str(name), str(value)) for name, value in event.headers or []]
                    path = urlsplit(dict(headers).get(":path", "")).path
                    block = reader.blocks.get(event.stream_id)
                    captured.requests.append(CapturedRequest(self._next(), path, headers, block=block))
                    _answer_stream(h2_connection, event.stream_id)
                    closing = closing or path in self.closing
                elif isinstance(event, h2.events.ConnectionTerminated):
                    return
            if closing:
                h2_connection.close_connection()
            connection.sendall(h2_connection.data_to_send())
            if closing:
                _linger(connection, reader)
                return

    def _settings_frame(self, connection: h2.connection.H2Connection) -> bytes:
        """The server's first SETTINGS frame: the settings h2 keeps that differ from the protocol's defaults.

        The values are h2's own, so what it enforces is what the client is told.
        """
        codes: list[h2.settings.SettingCodes] = list(_ANNOUNCED)
        if self._announces_header_table_size:
            codes.insert(0, h2.settings.SettingCodes.HEADER_TABLE_SIZE)
        payload = b"".join(code.to_bytes(2) + connection.local_settings[code].to_bytes(4) for code in codes)
        return len(payload).to_bytes(3) + bytes([4, 0]) + bytes(4) + payload


def _answer_stream(connection: h2.connection.H2Connection, stream_id: int) -> None:
    connection.send_headers(
        stream_id,
        [
            (":status", "200"),
            ("content-type", "text/html"),
            ("cache-control", "no-store"),
            ("content-length", str(len(PAGE))),
        ],
    )
    connection.send_data(stream_id, PAGE, end_stream=True)


def _linger(connection: ssl.SSLSocket, reader: Http2Reader) -> None:
    """Keep reading after GOAWAY until the client closes, for a while, so it reads the answer before the close."""
    connection.settimeout(_LINGER)
    try:
        while data := connection.recv(65536):
            reader.feed(data)
    except TimeoutError:
        pass


def _peek_client_hello(connection: socket.socket) -> bytes:
    """The records holding the client's ClientHello, left unread for the TLS handshake."""
    deadline = time.monotonic() + _PEEK_TIMEOUT
    while True:
        data = connection.recv(65536, socket.MSG_PEEK)
        size = client_hello_size(data)
        if size is not None:
            return data[:size]
        if not data or time.monotonic() > deadline:
            raise ConnectionAbortedError("the client did not send its whole ClientHello")
        time.sleep(0.001)


def _spki_hash(certificate: bytes) -> str:
    public_key = x509.load_pem_x509_certificate(certificate).public_key()
    spki = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return base64.b64encode(hashlib.sha256(spki).digest()).decode()
