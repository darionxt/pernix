"""Reading what a client puts on the wire: its TLS ClientHello, its HTTP/2 frames, and their HPACK representations.

These parsers keep what tells clients apart, such as the order of cipher suites or
whether a header is added to the HPACK dynamic table, which HTTP libraries decode
away. They read bytes as the loopback `WireServer` records them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

TLS_HANDSHAKE = 0x16
"""The content type of TLS records holding handshake messages, and the first byte of a TLS connection."""

GREASE = frozenset(0x0A0A + 0x1010 * n for n in range(16))
"""The values clients pick at random among for cipher suites, groups, versions, and extensions (RFC 8701)."""

HTTP2_PREFACE = b"PRI * HTTP/2.0\r\n\r\nSM\r\n\r\n"
"""What an HTTP/2 client sends first, before its frames."""

EXTENSIONS = {
    0: "server_name",
    5: "status_request",
    10: "supported_groups",
    11: "ec_point_formats",
    13: "signature_algorithms",
    16: "application_layer_protocol_negotiation",
    18: "signed_certificate_timestamp",
    21: "padding",
    23: "extended_master_secret",
    27: "compress_certificate",
    35: "session_ticket",
    41: "pre_shared_key",
    42: "early_data",
    43: "supported_versions",
    45: "psk_key_exchange_modes",
    51: "key_share",
    17513: "application_settings_old",
    17613: "application_settings",
    65037: "encrypted_client_hello",
    65281: "renegotiation_info",
}
"""TLS extension names by type, as IANA registers them."""

FRAME_TYPES = {
    0: "DATA",
    1: "HEADERS",
    2: "PRIORITY",
    3: "RST_STREAM",
    4: "SETTINGS",
    5: "PUSH_PROMISE",
    6: "PING",
    7: "GOAWAY",
    8: "WINDOW_UPDATE",
    9: "CONTINUATION",
    16: "PRIORITY_UPDATE",
}
"""HTTP/2 frame names by type."""

SETTINGS = {
    1: "HEADER_TABLE_SIZE",
    2: "ENABLE_PUSH",
    3: "MAX_CONCURRENT_STREAMS",
    4: "INITIAL_WINDOW_SIZE",
    5: "MAX_FRAME_SIZE",
    6: "MAX_HEADER_LIST_SIZE",
    8: "ENABLE_CONNECT_PROTOCOL",
    9: "NO_RFC7540_PRIORITIES",
}
"""HTTP/2 setting names by identifier."""

_ACK = 0x1
_END_STREAM = 0x1
_END_HEADERS = 0x4
_PADDED = 0x8
_PRIORITY = 0x20
"""Flags of HTTP/2 frames: ACK for SETTINGS and PING, the others for HEADERS."""


@dataclass(frozen=True)
class Extension:
    """A TLS extension of a ClientHello.

    Attributes:
        type: Its type, such as 0 for `server_name`.
        data: Its content.
    """

    type: int
    data: bytes

    @property
    def name(self) -> str:
        """Its IANA name, `GREASE`, or its type in hexadecimal."""
        if self.type in GREASE:
            return "GREASE"
        return EXTENSIONS.get(self.type, f"0x{self.type:04x}")


@dataclass(frozen=True)
class ClientHello:
    """The first message of a TLS handshake, as a client sent it.

    Attributes:
        records: The TLS records it came in.
        record_version: The version of its first record.
        version: The legacy version of the message.
        random: The client's random bytes.
        session_id: The legacy session id.
        cipher_suites: The cipher suites, in order.
        compression_methods: The compression methods, in order.
        extensions: The extensions, in order.
    """

    records: int
    record_version: int
    version: int
    random: bytes
    session_id: bytes
    cipher_suites: list[int]
    compression_methods: list[int]
    extensions: list[Extension]

    def extension(self, name: str) -> Extension | None:
        """Return the first extension of a name, if any.

        Args:
            name: Its IANA name, such as `pre_shared_key`.

        Returns:
            The extension, or `None` if the ClientHello has none of that name.
        """
        return next((extension for extension in self.extensions if extension.name == name), None)


def client_hello_size(data: bytes) -> int | None:
    """Return how many bytes of a TLS connection's start hold its whole ClientHello.

    Args:
        data: The first bytes the client sent.

    Returns:
        The size of the records holding the ClientHello, or `None` while it is not
        all there yet.

    Raises:
        ValueError: The bytes are not TLS handshake records.
    """
    found = _first_handshake(data)
    return None if found is None else found[2]


def parse_client_hello(data: bytes) -> ClientHello:
    """Parse a ClientHello from the TLS records holding it.

    Args:
        data: The first bytes the client sent, the whole ClientHello included.

    Returns:
        The ClientHello.

    Raises:
        ValueError: The bytes do not start with a whole ClientHello.
    """
    found = _first_handshake(data)
    if found is None:
        raise ValueError("the ClientHello is incomplete")
    handshake, records, _ = found
    if handshake[0] != 1:
        raise ValueError(f"not a ClientHello: handshake message type {handshake[0]}")
    reader = _Reader(handshake[4:])
    version = reader.integer(2)
    random = reader.take(32)
    session_id = reader.vector(1)
    suites = reader.vector(2)
    compression_methods = list(reader.vector(1))
    extensions: list[Extension] = []
    if reader.remaining:
        listed = _Reader(reader.vector(2))
        while listed.remaining:
            kind = listed.integer(2)
            extensions.append(Extension(kind, listed.vector(2)))
    return ClientHello(
        records=records,
        record_version=int.from_bytes(data[1:3]),
        version=version,
        random=random,
        session_id=session_id,
        cipher_suites=[int.from_bytes(suites[n : n + 2]) for n in range(0, len(suites), 2)],
        compression_methods=compression_methods,
        extensions=extensions,
    )


def _first_handshake(data: bytes) -> tuple[bytes, int, int] | None:
    """The first handshake message of a TLS connection, the records it came in, and their size, once all there."""
    handshake = b""
    records = size = 0
    while len(data) >= size + 5:
        if data[size] != TLS_HANDSHAKE:
            raise ValueError(f"not a TLS handshake record: {data[size : size + 5].hex()}")
        end = size + 5 + int.from_bytes(data[size + 3 : size + 5])
        if len(data) < end:
            return None
        handshake += data[size + 5 : end]
        records, size = records + 1, end
        if len(handshake) >= 4 and len(handshake) >= (length := 4 + int.from_bytes(handshake[1:4])):
            return handshake[:length], records, size
    return None


@dataclass(frozen=True)
class Frame:
    """An HTTP/2 frame a client sent.

    Attributes:
        type: Its type, such as 4 for SETTINGS.
        flags: Its flags.
        stream_id: Its stream.
        payload: Its payload.
    """

    type: int
    flags: int
    stream_id: int
    payload: bytes

    @property
    def name(self) -> str:
        """Its name, such as `SETTINGS`, or its type in hexadecimal."""
        return FRAME_TYPES.get(self.type, f"0x{self.type:02x}")

    @property
    def is_ack(self) -> bool:
        """Whether it acknowledges the peer's SETTINGS or PING."""
        return self.name in ("SETTINGS", "PING") and bool(self.flags & _ACK)

    def settings(self) -> list[tuple[int, int]]:
        """The settings of a SETTINGS frame, in order, repeated ones included."""
        return [
            (int.from_bytes(self.payload[n : n + 2]), int.from_bytes(self.payload[n + 2 : n + 6]))
            for n in range(0, len(self.payload), 6)
        ]

    def describe(self) -> str:
        """Its name and the fields that tell clients apart, such as `WINDOW_UPDATE on stream 0: 15663105`."""
        if self.name == "SETTINGS" and not self.is_ack:
            listed = ", ".join(f"{SETTINGS.get(key, f'0x{key:02x}')}={value}" for key, value in self.settings())
            return f"SETTINGS {listed}"
        if self.name == "WINDOW_UPDATE":
            return f"WINDOW_UPDATE on stream {self.stream_id}: {int.from_bytes(self.payload[:4]) & 0x7FFFFFFF}"
        if self.name == "PRIORITY":
            return f"PRIORITY on stream {self.stream_id}: {_priority(self.payload[:5])}"
        return f"{self.name} on stream {self.stream_id}, flags 0x{self.flags:02x}"


@dataclass(frozen=True)
class Representation:
    """How an HPACK header block represents one header field, or a dynamic table size update.

    Attributes:
        kind: `indexed`, a literal `with incremental indexing`, `without indexing`,
            or `never indexed`, or a `size update`.
        index: The index of the field, or of its name; 0 for a literal name; the
            new size for a size update.
        name_huffman: Whether a literal name is Huffman-coded; `None` without one.
        value_huffman: Whether a literal value is Huffman-coded; `None` without one.
    """

    kind: str
    index: int
    name_huffman: bool | None = None
    value_huffman: bool | None = None

    def __str__(self) -> str:
        if self.kind == "indexed":
            return f"indexed {self.index}"
        if self.kind == "size update":
            return f"dynamic table size update to {self.index}"
        name = f"name {self.index}" if self.index else f"literal name{' (Huffman)' if self.name_huffman else ''}"
        return f"literal {self.kind}, {name}, value{' (Huffman)' if self.value_huffman else ''}"


@dataclass(frozen=True)
class HeaderBlock:
    """A HEADERS frame a client sent, with the CONTINUATION frames that end its header block.

    Attributes:
        stream_id: Its stream.
        flags: The flags of the HEADERS frame.
        priority: Its priority fields, as `exclusive on N, weight W`, if it has any.
        padding: The bytes of padding.
        continuations: The CONTINUATION frames that followed it.
        representations: How the block represents each header field.
    """

    stream_id: int
    flags: int
    priority: str | None
    padding: int
    continuations: int
    representations: list[Representation]

    def describe(self) -> str:
        """The frame's fields, such as `stream 1, END_STREAM END_HEADERS PRIORITY, exclusive on 0, weight 256`."""
        names = [
            name
            for flag, name in (
                (_END_STREAM, "END_STREAM"),
                (_END_HEADERS, "END_HEADERS"),
                (_PADDED, "PADDED"),
                (_PRIORITY, "PRIORITY"),
            )
            if self.flags & flag
        ]
        others = self.flags & ~(_END_STREAM | _END_HEADERS | _PADDED | _PRIORITY)
        if others:
            names.append(f"0x{others:02x}")
        parts = [f"stream {self.stream_id}", " ".join(names) or "no flags"]
        if self.priority is not None:
            parts.append(self.priority)
        if self.padding:
            parts.append(f"{self.padding} bytes of padding")
        if self.continuations:
            parts.append(f"{self.continuations} CONTINUATION frames")
        return ", ".join(parts)


class Http2Reader:
    """Reads the frames a client sends over HTTP/2, as they arrive, after checking its preface.

    Attributes:
        frames: The frames read so far, in order.
        blocks: The header blocks read so far, by stream, a HEADERS frame with the
            CONTINUATION frames ending it; a later block on a stream, such as
            trailers, is not kept.
    """

    def __init__(self) -> None:
        self.frames: list[Frame] = []
        self.blocks: dict[int, HeaderBlock] = {}
        self._buffer = b""
        self._preface = False
        self._open: list[Frame] = []

    def feed(self, data: bytes) -> None:
        """Read more bytes of the connection.

        Args:
            data: The bytes, decrypted.

        Raises:
            ValueError: The connection does not start with the HTTP/2 preface.
        """
        self._buffer += data
        if not self._preface:
            if len(self._buffer) < len(HTTP2_PREFACE):
                return
            if not self._buffer.startswith(HTTP2_PREFACE):
                raise ValueError(f"not an HTTP/2 preface: {self._buffer[:24]!r}")
            self._buffer = self._buffer[len(HTTP2_PREFACE) :]
            self._preface = True
        for frame in self._split():
            self.frames.append(frame)
            if frame.name in ("HEADERS", "CONTINUATION"):
                self._open.append(frame)
                if frame.flags & _END_HEADERS:
                    self._end_block()

    def _split(self) -> Iterator[Frame]:
        while len(self._buffer) >= 9:
            end = 9 + int.from_bytes(self._buffer[:3])
            if len(self._buffer) < end:
                return
            head, payload, self._buffer = self._buffer[:9], self._buffer[9:end], self._buffer[end:]
            yield Frame(head[3], head[4], int.from_bytes(head[5:9]) & 0x7FFFFFFF, payload)

    def _end_block(self) -> None:
        first, *continuations = self._open
        self._open = []
        payload = first.payload
        padding = payload[0] if first.flags & _PADDED else 0
        start = 1 if first.flags & _PADDED else 0
        priority = None
        if first.flags & _PRIORITY:
            priority = _priority(payload[start : start + 5])
            start += 5
        fragment = payload[start : len(payload) - padding] + b"".join(frame.payload for frame in continuations)
        block = HeaderBlock(
            first.stream_id, first.flags, priority, padding, len(continuations), representations(fragment)
        )
        self.blocks.setdefault(first.stream_id, block)


def representations(block: bytes) -> list[Representation]:
    """Tell how an HPACK header block represents each of its fields (RFC 7541 §6).

    The strings are skipped, not decoded: header names and values are read by an
    HTTP/2 library, which keeps the dynamic table.

    Args:
        block: The header block.

    Returns:
        The representations, in order, dynamic table size updates included.
    """
    reader = _Reader(block)
    found: list[Representation] = []
    while reader.remaining:
        first = reader.peek()
        if first & 0x80:
            found.append(Representation("indexed", reader.hpack_integer(7)))
            continue
        if first & 0xE0 == 0x20:
            found.append(Representation("size update", reader.hpack_integer(5)))
            continue
        if first & 0xC0 == 0x40:
            kind, index = "with incremental indexing", reader.hpack_integer(6)
        else:
            kind, index = "never indexed" if first & 0x10 else "without indexing", reader.hpack_integer(4)
        name_huffman = None if index else reader.hpack_string()
        found.append(Representation(kind, index, name_huffman, reader.hpack_string()))
    return found


def _priority(fields: bytes) -> str:
    dependency = int.from_bytes(fields[:4])
    exclusive = "exclusive on" if dependency & 0x80000000 else "on"
    return f"{exclusive} {dependency & 0x7FFFFFFF}, weight {fields[4] + 1}"


class _Reader:
    """Reads a message field by field."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._position

    def peek(self) -> int:
        return self._data[self._position]

    def take(self, size: int) -> bytes:
        if size > self.remaining:
            raise ValueError("the message is truncated")
        taken = self._data[self._position : self._position + size]
        self._position += size
        return taken

    def integer(self, size: int) -> int:
        return int.from_bytes(self.take(size))

    def vector(self, length_size: int) -> bytes:
        """A field preceded by its length, as TLS encodes vectors."""
        return self.take(self.integer(length_size))

    def hpack_integer(self, prefix: int) -> int:
        """An integer with an N-bit prefix (RFC 7541 §5.1)."""
        mask = (1 << prefix) - 1
        value = self.integer(1) & mask
        if value < mask:
            return value
        shift = 0
        while True:
            byte = self.integer(1)
            value += (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return value

    def hpack_string(self) -> bool:
        """Skip a string literal (RFC 7541 §5.2), telling whether it is Huffman-coded."""
        huffman = bool(self.peek() & 0x80)
        self.take(self.hpack_integer(7))
        return huffman
