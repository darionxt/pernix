"""Reading ClientHellos, HTTP/2 frames, and HPACK representations from the bytes a client sent."""

from __future__ import annotations

import ssl

import h2.config
import h2.connection
import pytest

from wire_capture import (
    TLS_HANDSHAKE,
    Http2Reader,
    Representation,
    client_hello_size,
    parse_client_hello,
    representations,
)


def _client_hello() -> bytes:
    """The ClientHello Python's own TLS client sends to localhost, offering HTTP/2."""
    context = ssl.create_default_context()
    context.set_alpn_protocols(["h2", "http/1.1"])
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(incoming, outgoing, server_hostname="localhost")
    with pytest.raises(ssl.SSLWantReadError):
        client.do_handshake()
    return outgoing.read()


def _split(records: bytes, at: int) -> bytes:
    """The handshake of one TLS record sent in two records, split at a byte of its body."""
    body = records[5:]
    version = records[1:3]
    return b"".join(bytes([TLS_HANDSHAKE]) + version + len(part).to_bytes(2) + part for part in (body[:at], body[at:]))


def _h2_client() -> h2.connection.H2Connection:
    client = h2.connection.H2Connection(h2.config.H2Configuration(client_side=True, header_encoding="utf-8"))
    client.initiate_connection()
    return client


HEADERS = [(":method", "GET"), (":authority", "localhost"), (":scheme", "https"), (":path", "/"), ("accept", "*/*")]


def test_a_client_hello_is_read_field_by_field() -> None:
    hello = parse_client_hello(_client_hello())

    assert (hello.records, hello.record_version, hello.version) == (1, 0x0301, 0x0303)
    assert (len(hello.random), len(hello.session_id)) == (32, 32)
    assert 0x1301 in hello.cipher_suites
    assert hello.compression_methods == [0]
    server_name, alpn = hello.extension("server_name"), hello.extension("application_layer_protocol_negotiation")
    assert server_name is not None
    assert server_name.data.endswith(b"localhost")
    assert alpn is not None
    assert alpn.data == b"\x00\x0c\x02h2\x08http/1.1"
    assert hello.extension("pre_shared_key") is None


def test_a_client_hello_split_across_records_is_read_whole() -> None:
    records = _client_hello()
    split = _split(records, 100)

    assert client_hello_size(split[:120]) is None
    assert client_hello_size(split + b"more") == len(split)
    hello = parse_client_hello(split)
    assert hello.records == 2
    assert hello.extensions == parse_client_hello(records).extensions


def test_bytes_that_are_not_a_tls_handshake_are_refused() -> None:
    with pytest.raises(ValueError, match="not a TLS handshake record"):
        client_hello_size(b"GET / HTTP/1.1\r\n\r\n")


def test_http2_frames_are_read_in_order_with_each_header_block() -> None:
    client = _h2_client()
    client.send_headers(
        1, HEADERS, end_stream=True, priority_weight=256, priority_depends_on=0, priority_exclusive=True
    )
    reader = Http2Reader()

    sent = client.data_to_send()
    for start in range(0, len(sent), 7):  # as it may arrive, a few bytes at a time
        reader.feed(sent[start : start + 7])

    assert [frame.name for frame in reader.frames] == ["SETTINGS", "HEADERS"]
    assert reader.frames[0].describe() == (
        "SETTINGS HEADER_TABLE_SIZE=4096, ENABLE_PUSH=1, INITIAL_WINDOW_SIZE=65535, MAX_FRAME_SIZE=16384, "
        "ENABLE_CONNECT_PROTOCOL=0, MAX_CONCURRENT_STREAMS=100, MAX_HEADER_LIST_SIZE=65536"
    )
    block = reader.blocks[1]
    assert block.describe() == "stream 1, END_STREAM END_HEADERS PRIORITY, exclusive on 0, weight 256"
    assert len(block.representations) == len(HEADERS)


def test_a_later_header_block_refers_to_the_dynamic_table() -> None:
    client = _h2_client()
    client.send_headers(1, HEADERS, end_stream=True)
    client.send_headers(3, HEADERS, end_stream=True)
    reader = Http2Reader()

    reader.feed(client.data_to_send())

    first, second = reader.blocks[1].representations, reader.blocks[3].representations
    assert first[:2] == [Representation("indexed", 2), Representation("with incremental indexing", 1, None, True)]
    assert [str(representation) for representation in second] == [
        "indexed 2",
        "indexed 63",
        "indexed 7",
        "indexed 4",
        "indexed 62",
    ]


def test_a_header_block_too_large_for_one_frame_ends_in_continuation_frames() -> None:
    client = _h2_client()
    client.send_headers(1, [*HEADERS, ("x-large", "~" * 15000)], end_stream=True)
    reader = Http2Reader()

    reader.feed(client.data_to_send())

    assert [frame.name for frame in reader.frames] == ["SETTINGS", "HEADERS", "CONTINUATION"]
    block = reader.blocks[1]
    assert block.continuations == 1
    assert len(block.representations) == len(HEADERS) + 1


def test_a_connection_without_the_http2_preface_is_refused() -> None:
    with pytest.raises(ValueError, match="not an HTTP/2 preface"):
        Http2Reader().feed(b"GET / HTTP/1.1\r\nHost: localhost\r\n\r\n")


def test_each_kind_of_hpack_representation_is_told_apart() -> None:
    block = bytes(
        [
            0x3F, 0xE1, 0x1F,  # a size update to 4096, past the 5-bit prefix
            0x82,  # :method GET
            0x44, 0x81, 0x63,  # :path, its value Huffman-coded
            0x00, 0x01, 0x61, 0x01, 0x62,  # a literal name, without indexing
            0x1F, 0x11, 0x01, 0x63,  # never indexed, a name past the 4-bit prefix
        ]
    )  # fmt: skip

    assert representations(block) == [
        Representation("size update", 4096),
        Representation("indexed", 2),
        Representation("with incremental indexing", 4, None, True),
        Representation("without indexing", 0, False, False),
        Representation("never indexed", 32, None, False),
    ]
    assert str(representations(block)[3]) == "literal without indexing, literal name, value"
