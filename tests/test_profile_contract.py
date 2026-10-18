"""The Profile contract: the HTTP Fetcher and the browser, under one Session, present one client on the wire.

The pinned pair is the Chrome for Testing build bundled with the pinned patchright
and the default Profile of the pinned curl-cffi (ADR 0010). An upgrade of either
lands only with these tests passing on macOS and Linux.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from pernix import Session
from profile_contract import Capture, Step, assert_one_client, capture_http, differences, run_both
from wire_server import WireServer

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

CASES = {
    "a first connection": (Step("/"),),
    "a second request on one connection": (Step("/one"), Step("/two")),
    "a resumed connection": (Step("/one", closes=True), Step("/two")),
}
"""Top-level navigations, by what they show."""


@pytest.fixture
def ca(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> trustme.CA:
    """A certificate authority that curl trusts, and that the server's certificate comes from."""
    ca = trustme.CA()
    bundle = tmp_path / "ca.pem"
    ca.cert_pem.write_to_path(str(bundle))
    monkeypatch.setattr(curl_cffi.aio, "DEFAULT_CACERT", str(bundle))  # read by each new async curl session
    return ca


@pytest.fixture
def server(ca: trustme.CA) -> Iterator[WireServer]:
    with WireServer(ca) as server:
        yield server


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.parametrize("steps", CASES.values(), ids=CASES.keys())
async def test_the_http_fetcher_and_the_browser_present_one_client(server: WireServer, steps: tuple[Step, ...]) -> None:
    http, browser = await run_both(server, steps)

    resumed = [False] + [True] * sum(step.closes for step in steps)
    assert http.resumptions() == browser.resumptions() == resumed
    assert_one_client(http, browser)


@pytest.mark.usefixtures("browser_engine")
async def test_a_profile_of_another_chrome_version_breaks_the_contract(server: WireServer) -> None:
    http, browser = await run_both(server, [Step("/")], impersonate="chrome99")

    found = "\n".join(differences(http, browser))
    assert "connection 1 > HTTP/2 preface:\n" in found
    assert "-SETTINGS HEADER_TABLE_SIZE=65536, MAX_CONCURRENT_STREAMS=1000," in found
    assert "request 1 > headers:\n" in found
    assert "+priority: u=0, i" in found


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.xfail(
    reason="once a server announces SETTINGS_HEADER_TABLE_SIZE, curl's HPACK encoder starts its next header block "
    "with a dynamic table size update, even when the size is unchanged; Chrome sends one only when it changes",
    strict=True,
)
async def test_both_encode_headers_alike_for_a_server_announcing_its_header_table_size(ca: trustme.CA) -> None:
    with WireServer(ca, announces_header_table_size=True) as server:
        http, browser = await run_both(server, [Step("/one"), Step("/two")])

    assert_one_client(http, browser)


async def test_two_captures_of_one_profile_show_no_difference(server: WireServer) -> None:
    steps = CASES["a resumed connection"]

    first = await capture_http(server, steps, Session())
    second = await capture_http(server, steps, Session())

    assert differences(first, second) == []  # what each connection randomizes is left out


async def test_an_http2_setting_changed_on_one_side_is_named(server: WireServer) -> None:
    http = await capture_http(server, [Step("/")], Session())
    [connection] = http.connections
    settings, *others = connection.frames
    changed = dataclasses.replace(
        settings, payload=settings.payload.replace(b"\x00\x04\x00\x60\x00\x00", b"\x00\x04\x00\x01\x00\x00")
    )

    found = differences(http, Capture([dataclasses.replace(connection, frames=[changed, *others])]))

    assert found == [
        "connection 1 > HTTP/2 preface:\n"
        "  --- the HTTP Fetcher\n"
        "  +++ the browser\n"
        "  @@ -1,2 +1,2 @@\n"
        "  -SETTINGS HEADER_TABLE_SIZE=65536, ENABLE_PUSH=0, INITIAL_WINDOW_SIZE=6291456, MAX_HEADER_LIST_SIZE=262144\n"
        "  +SETTINGS HEADER_TABLE_SIZE=65536, ENABLE_PUSH=0, INITIAL_WINDOW_SIZE=65536, MAX_HEADER_LIST_SIZE=262144\n"
        "   WINDOW_UPDATE on stream 0: 15663105"
    ]


async def test_a_header_order_changed_on_one_side_is_named(server: WireServer) -> None:
    http = await capture_http(server, [Step("/")], Session())
    [connection] = http.connections
    [request] = connection.requests
    names = [name for name, _ in request.headers]
    fetch_site, fetch_mode = names.index("sec-fetch-site"), names.index("sec-fetch-mode")
    swapped = list(request.headers)
    swapped[fetch_site], swapped[fetch_mode] = swapped[fetch_mode], swapped[fetch_site]
    reordered = dataclasses.replace(connection, requests=[dataclasses.replace(request, headers=swapped)])

    [found] = differences(http, Capture([reordered]))

    assert found.startswith("request 1 > headers:\n")
    assert "\n  +sec-fetch-mode: navigate\n   sec-fetch-site: none\n  -sec-fetch-mode: navigate\n" in found


async def test_the_values_of_identity_headers_are_left_out_for_now(server: WireServer) -> None:
    http = await capture_http(server, [Step("/")], Session())
    [connection] = http.connections
    [request] = connection.requests
    renamed = [(name, "Other/1.0" if name == "user-agent" else value) for name, value in request.headers]
    changed = dataclasses.replace(connection, requests=[dataclasses.replace(request, headers=renamed)])

    assert differences(http, Capture([changed])) == []
