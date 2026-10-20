"""The Profile contract: under one Session, the HTTP Fetcher and the browser Fetcher present one client on the wire.

`run_both()` makes the requests of a case with each Fetcher in turn, from a
`WireServer`, and `differences()` compares what each put on the wire, naming every
difference: the TLS ClientHello of each connection, its HTTP/2 preface, and, for
each request, the connection it went on, its HEADERS frame, the HPACK
representation of each field, and the fields in order with their values.

Left out of the comparison:

- GREASE values, random for each connection; where they are is compared.
- The ClientHello's random, its legacy session id (but its length), and the keys
  of its key shares, all random.
- The order of TLS extensions, which both clients shuffle for each connection.
- The config id, the encapsulated key, and the payload of the GREASE
  `encrypted_client_hello` extension, the payload's length included, all random.
- The identities, ticket ages, and binders of `pre_shared_key`, which come from
  the server's ticket; their number and the binders' lengths are compared.
- The length of the `padding` extension, and of the records, which follow from
  the rest.
- SETTINGS and PING acknowledgements, sent whenever the server's frames arrive.
- The frames after the first HEADERS frame of a connection other than HEADERS,
  such as WINDOW_UPDATE and GOAWAY, which follow the sizes of the answers and how
  the connection ends.
- Connections that carry no request, which a browser may open ahead of need.

The server announces no `SETTINGS_HEADER_TABLE_SIZE`, as many servers do not:
curl's HPACK encoder answers one with a dynamic table size update that Chrome
sends only when the size changes.
"""

from __future__ import annotations

import asyncio
import difflib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pernix import Request, Session
from pernix.browser import Render
from pernix.fetchers import BrowserFetcher, BrowserSettings, HTTPFetcher
from pernix.settings import DEFAULT_LOCALE
from wire_capture import GREASE
from wire_server import HOST

if TYPE_CHECKING:
    from collections.abc import Collection, Iterator, Mapping, Sequence

    from pernix.browser import Action
    from wire_capture import ClientHello, Extension, Frame
    from wire_server import CapturedConnection, CapturedRequest, WireServer

type Observed = str | tuple[str, ...]
"""What a capture shows under one name: a value, or lines compared as a list."""

_CLOSE_TIMEOUT = 5.0
"""Seconds a Fetcher's connections may take to close once the Fetcher is closed."""


@dataclass(frozen=True)
class Step:
    """One request of a case.

    The HTTP Fetcher sends it as a Request. The browser loads it as a page, running
    `actions` on it, unless a page an earlier step loaded makes it, as its links,
    frames, forms, scripts, and loads do.

    Attributes:
        path: The path.
        closes: Whether the server closes the connection once it answered, so the
            next step connects again, resuming the TLS session.
        host: The host name it goes to: the server's own, or another, such as
            `127.0.0.1` for another site.
        scheme: `https`, or `http` for cleartext HTTP/1.1.
        method: The method.
        body: The body.
        headers: The Request's own headers, such as `Sec-Fetch-Dest`, which names
            its kind.
        referrer: The path of the page it comes from, which the Request's `Referer`
            names; `None` for none.
        referrer_host: The host name of that page.
        by_page: Whether the browser makes it from the page of an earlier step.
        actions: What the browser does on the page once it loaded, such as clicking
            a link.
    """

    path: str
    closes: bool = False
    host: str = HOST
    scheme: str = "https"
    method: str = "GET"
    body: bytes | None = None
    headers: tuple[tuple[str, str], ...] = ()
    referrer: str | None = None
    referrer_host: str = HOST
    by_page: bool = False
    actions: tuple[Action, ...] = ()

    def url(self, server: WireServer) -> str:
        """Its URL on a server."""
        return server.url(self.path, scheme=self.scheme, host=self.host)

    def request(self, server: WireServer, *, impersonate: str | None) -> Request:
        """The Request the HTTP Fetcher sends for it."""
        headers = list(self.headers)
        if self.referrer is not None:
            headers.append(("Referer", server.url(self.referrer, scheme=self.scheme, host=self.referrer_host)))
        return Request(self.url(server), method=self.method, body=self.body, headers=headers, impersonate=impersonate)


@dataclass(frozen=True)
class Capture:
    """What one Fetcher put on the wire for the steps of a case.

    Attributes:
        connections: The connections that carried its requests, in the order of
            their first request.
    """

    connections: list[CapturedConnection]

    def requests(self) -> list[tuple[int, CapturedRequest]]:
        """Each request, in the order the server received them, with the number of its connection, from 1."""
        numbered = [
            (number, request)
            for number, connection in enumerate(self.connections, 1)
            for request in connection.requests
        ]
        return sorted(numbered, key=lambda pair: pair[1].sequence)

    def resumptions(self) -> list[bool]:
        """Whether each connection resumed a TLS session."""
        return [connection.resumed for connection in self.connections]


async def run_both(
    server: WireServer,
    steps: Sequence[Step],
    *,
    impersonate: str | None = None,
    locale: Sequence[str] = DEFAULT_LOCALE,
    timezone: str | None = None,
    launch_options: Mapping[str, Any] | None = None,
) -> tuple[Capture, Capture]:
    """Make the requests of a case with the HTTP Fetcher, then with the browser Fetcher, under one Session.

    Args:
        server: The server, whose CA curl trusts.
        steps: The requests, in order.
        impersonate: The Profile the HTTP Fetcher uses instead of the default one.
        locale: The languages both present.
        timezone: The time zone the browser presents; the machine's when `None`.
        launch_options: Options the browser is launched with, over those that make
            it trust the server.

    Returns:
        What the HTTP Fetcher put on the wire, and what the browser did.
    """
    session = Session()
    http = await capture_http(server, steps, session, impersonate=impersonate, locale=locale)
    browser = await capture_browser(
        server, steps, session, locale=locale, timezone=timezone, launch_options=launch_options
    )
    return http, browser


async def capture_http(
    server: WireServer,
    steps: Sequence[Step],
    session: Session,
    *,
    impersonate: str | None = None,
    locale: Sequence[str] = DEFAULT_LOCALE,
) -> Capture:
    """Make the requests of a case with an HTTP Fetcher of its own.

    Args:
        server: The server, whose CA curl trusts.
        steps: The requests, in order.
        session: The Session to send them under.
        impersonate: The Profile to use instead of the default one.
        locale: The languages to present.

    Returns:
        What the HTTP Fetcher put on the wire.
    """
    since = _closing(server, steps)
    async with HTTPFetcher(locale=locale) as fetcher:
        for step in steps:
            await fetcher.fetch(step.request(server, impersonate=impersonate), session=session)
            await _after(server, step)
    return await _captured(server, since)


async def capture_browser(
    server: WireServer,
    steps: Sequence[Step],
    session: Session,
    *,
    locale: Sequence[str] = DEFAULT_LOCALE,
    timezone: str | None = None,
    launch_options: Mapping[str, Any] | None = None,
) -> Capture:
    """Make the requests of a case with a browser Fetcher of its own, which trusts the server's certificate.

    Args:
        server: The server.
        steps: The requests, in order; those an earlier step's page makes are not
            loaded.
        session: The Session to load them under.
        locale: The languages to present.
        timezone: The time zone to present; the machine's when `None`.
        launch_options: Options the browser is launched with, over those that make
            it trust the server.

    Returns:
        What the browser put on the wire.
    """
    since = _closing(server, steps)
    options = dict(launch_options or {})
    options["args"] = [f"--ignore-certificate-errors-spki-list={server.spki}", *options.get("args", ())]
    async with BrowserFetcher(BrowserSettings(launch_options=options), locale=locale, timezone=timezone) as fetcher:
        for step in steps:
            if step.by_page:
                continue
            render = Render(actions=step.actions) if step.actions else True
            await fetcher.fetch(Request(step.url(server), render=render), session=session)
            await _after(server, step)
    return await _captured(server, since)


def differences(http: Capture, browser: Capture, *, leaving_out: Collection[str] = ()) -> list[str]:
    """Compare what the two Fetchers put on the wire.

    Args:
        http: What the HTTP Fetcher did.
        browser: What the browser Fetcher did.
        leaving_out: The observations not compared, by the end of their names,
            such as `HPACK`, for a known difference.

    Returns:
        Each difference, naming what differs and showing both sides; none when
        both presented one client.
    """
    sent, shown = describe(http), describe(browser)
    found: list[str] = []
    for name in dict.fromkeys([*sent, *shown]):
        if any(name.endswith(f"> {left_out}") for left_out in leaving_out):
            continue
        ours, theirs = sent.get(name), shown.get(name)
        if ours == theirs:
            continue
        if ours is None:
            found.append(f"{name}: only the browser sent{_shown(theirs)}")
        elif theirs is None:
            found.append(f"{name}: only the HTTP Fetcher sent{_shown(ours)}")
        elif isinstance(ours, tuple) or isinstance(theirs, tuple):
            lines = difflib.unified_diff(_lines(ours), _lines(theirs), "the HTTP Fetcher", "the browser", lineterm="")
            found.append(f"{name}:\n" + "\n".join(f"  {line}" for line in lines))
        else:
            found.append(f"{name}: the HTTP Fetcher sent {ours}, the browser {theirs}")
    return found


def assert_one_client(http: Capture, browser: Capture, *, leaving_out: Collection[str] = ()) -> None:
    """Fail unless the two Fetchers presented one client.

    Args:
        http: What the HTTP Fetcher did.
        browser: What the browser Fetcher did.
        leaving_out: The observations not compared, by the end of their names.

    Raises:
        AssertionError: They differ; the message names each difference.
    """
    found = differences(http, browser, leaving_out=leaving_out)
    assert not found, "the HTTP Fetcher and the browser differ on the wire:\n" + "\n".join(found)


def describe(capture: Capture) -> dict[str, Observed]:
    """What a capture shows, by name, without what no client can make match.

    Args:
        capture: What one Fetcher put on the wire.

    Returns:
        The observations, such as `connection 1 > ClientHello > cipher suites`, in
        the order they were made.
    """
    observed: dict[str, Observed] = {}
    for number, connection in enumerate(capture.connections, 1):
        prefix = f"connection {number}"
        cleartext = connection.client_hello is None
        observed[f"{prefix} > protocol"] = f"{connection.protocol}{' in cleartext' if cleartext else ' over TLS'}"
        if connection.client_hello is not None:
            observed |= _client_hello(f"{prefix} > ClientHello", connection.client_hello)
        if connection.protocol == "HTTP/2":
            observed[f"{prefix} > HTTP/2 preface"] = tuple(_preface(connection.frames))
    on_connection: dict[int, int] = {}
    for index, (number, request) in enumerate(capture.requests(), 1):
        prefix = f"request {index}"
        on_connection[number] = on_connection.get(number, 0) + 1
        observed[f"{prefix} > path"] = request.path
        observed[f"{prefix} > connection"] = f"connection {number}, request {on_connection[number]} on it"
        if request.request_line is not None:
            observed[f"{prefix} > request line"] = request.request_line
        if request.block is not None:
            observed[f"{prefix} > HEADERS frame"] = request.block.describe()
            observed[f"{prefix} > HPACK"] = tuple(
                str(representation) for representation in request.block.representations
            )
        observed[f"{prefix} > headers"] = tuple(f"{name}: {value}" for name, value in request.headers)
    return observed


def _client_hello(prefix: str, hello: ClientHello) -> dict[str, Observed]:
    observed: dict[str, Observed] = {
        f"{prefix} > records": f"{hello.records} of version {_code(hello.record_version)}",
        f"{prefix} > legacy version": _code(hello.version),
        f"{prefix} > legacy session id": f"{len(hello.session_id)} bytes",
        f"{prefix} > cipher suites": tuple(_code(suite) for suite in hello.cipher_suites),
        f"{prefix} > compression methods": tuple(str(method) for method in hello.compression_methods),
        f"{prefix} > extensions": tuple(sorted(extension.name for extension in hello.extensions)),
    }
    greased = tuple(extension.data.hex() or "empty" for extension in hello.extensions if extension.name == "GREASE")
    if greased:
        observed[f"{prefix} > GREASE extensions"] = greased
    for extension in sorted(hello.extensions, key=lambda extension: extension.name):
        if extension.name != "GREASE":
            observed[f"{prefix} > {extension.name}"] = _extension(extension)
    return observed


def _extension(extension: Extension) -> Observed:
    """The content of a TLS extension, without what is random or the server's own."""
    data = extension.data
    match extension.name:
        case "supported_groups" | "signature_algorithms":
            return tuple(_code(code) for code in _codes(data[2:]))
        case "supported_versions":
            return tuple(_code(code) for code in _codes(data[1:]))
        case "compress_certificate":
            return tuple(_code(code) for code in _codes(data[1:]))
        case "key_share":
            return tuple(f"{_code(group)}, {len(key)} bytes" for group, key in _entries(data[2:], 2, 2))
        case "application_layer_protocol_negotiation" | "application_settings" | "application_settings_old":
            return tuple(name.decode() for _, name in _entries(data[2:], 0, 1))
        case "server_name":
            return tuple(name.decode() for _, name in _entries(data[2:], 1, 2))
        case "encrypted_client_hello":
            if data[0] != 0:
                return f"type {data[0]}"
            enc = int.from_bytes(data[6:8])
            return f"outer, KDF {_code(int.from_bytes(data[1:3]))}, AEAD {_code(int.from_bytes(data[3:5]))}, {enc} bytes of enc"
        case "pre_shared_key":
            identities = data[2 : 2 + int.from_bytes(data[:2])]
            binders = data[4 + len(identities) :]
            count = sum(1 for _ in _entries(identities, 0, 2, trailer=4))
            return f"{count} identities, binders of {[len(binder) for _, binder in _entries(binders, 0, 1)]} bytes"
        case "padding":
            return "present"
        case _:
            return data.hex() or "empty"


def _preface(frames: list[Frame]) -> Iterator[str]:
    """The frames a client sent before its first HEADERS frame, acknowledgements left out."""
    for frame in frames:
        if frame.name == "HEADERS":
            return
        if not frame.is_ack:
            yield frame.describe()


def _code(value: int) -> str:
    return "GREASE" if value in GREASE else f"0x{value:04x}"


def _codes(data: bytes) -> list[int]:
    return [int.from_bytes(data[n : n + 2]) for n in range(0, len(data) - 1, 2)]


def _entries(data: bytes, key_size: int, length_size: int, *, trailer: int = 0) -> Iterator[tuple[int, bytes]]:
    """The entries of a TLS list: each a key of `key_size` bytes, then a vector, then `trailer` bytes."""
    position = 0
    while position < len(data):
        key = int.from_bytes(data[position : position + key_size])
        position += key_size
        size = int.from_bytes(data[position : position + length_size])
        position += length_size
        yield key, data[position : position + size]
        position += size + trailer


def _shown(observed: Observed | None) -> str:
    if isinstance(observed, tuple):
        return ":" + "".join(f"\n  {line}" for line in observed)
    return f" {observed}"


def _lines(observed: Observed) -> list[str]:
    return list(observed) if isinstance(observed, tuple) else [observed]


def _closing(server: WireServer, steps: Sequence[Step]) -> int:
    """Have the server close the connection after the steps that close it; the connections it accepted so far."""
    server.closing.update(step.path for step in steps if step.closes)
    return len(server.connections)


async def _after(server: WireServer, step: Step) -> None:
    """Wait for the server to close the connection of a step that closes it."""
    if step.closes:
        await server.closed_after(step.path)


async def _captured(server: WireServer, since: int) -> Capture:
    """The connections accepted since a count, once those of the closed Fetcher are closed."""
    connections = server.connections[since:]
    deadline = time.monotonic() + _CLOSE_TIMEOUT
    for connection in connections:
        await asyncio.to_thread(connection.closed.wait, max(0.0, deadline - time.monotonic()))
    used = [connection for connection in connections if connection.requests]
    return Capture(sorted(used, key=lambda connection: connection.requests[0].sequence))
