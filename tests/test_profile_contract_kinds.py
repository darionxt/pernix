"""The Profile contract for each kind of request: what the HTTP Fetcher sends is what a page makes Chrome send.

The browser loads a page that makes the request, through a link, a frame, a form,
a script, or a load, and the HTTP Fetcher sends the same page's request and then a
Request of that kind, whose `Referer` names the page.
"""

from __future__ import annotations

import base64
from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from pernix.browser import Click, Evaluate
from pernix.fetchers import http_fetcher
from profile_contract import Step, assert_one_client, run_both
from wire_server import WireServer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("browser_engine")

OTHER_SITE = "127.0.0.1"
"""The server under another name, which is another site than `localhost`."""

INSECURE = "insecure.test"
"""A host name that is no loopback one, which Chrome takes for an insecure origin over `http://`."""

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

type Case = Callable[[WireServer], tuple[Step, ...]]
"""Routes a server's pages, and gives the steps of a case."""


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


def _page(server: WireServer, path: str, body: str) -> None:
    server.routes[path] = ("text/html", f'<!doctype html><link rel="icon" href="data:,">{body}'.encode())


def _link(host: str) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        _page(server, "/links", f'<a id="go" href="{server.url("/target", host=host)}">go</a>')
        return (Step("/links", actions=(Click("#go"),)), Step("/target", host=host, referrer="/links", by_page=True))

    return case


def _iframe(host: str) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        _page(server, "/frames", f'<iframe src="{server.url("/frame", host=host)}"></iframe>')
        dest = (("Sec-Fetch-Dest", "iframe"),)
        return (Step("/frames"), Step("/frame", host=host, headers=dest, referrer="/frames", by_page=True))

    return case


def _form(server: WireServer) -> tuple[Step, ...]:
    _page(
        server,
        "/form",
        '<form method="post" action="/submit"><input name="a" value="1"><button id="go">go</button></form>',
    )
    form = (("Content-Type", "application/x-www-form-urlencoded"),)
    return (
        Step("/form", actions=(Click("#go"),)),
        Step("/submit", method="POST", body=b"a=1", headers=form, referrer="/form", by_page=True),
    )


def _fetch(host: str, *, scheme: str = "https", page_host: str = "localhost") -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        server.routes["/api"] = ("application/json", b"{}")
        target = server.url("/api", scheme=scheme, host=host)
        _page(server, "/fetching", f'<script>window.done = fetch("{target}").then(() => 1, () => 0)</script>')
        return (
            Step("/fetching", host=page_host, scheme=scheme, actions=(Evaluate("window.done"),)),
            Step(
                "/api",
                host=host,
                scheme=scheme,
                headers=(("Sec-Fetch-Dest", "empty"),),
                referrer="/fetching",
                referrer_host=page_host,
                by_page=True,
            ),
        )

    return case


def _json_post(server: WireServer) -> tuple[Step, ...]:
    server.routes["/api"] = ("application/json", b"{}")
    script = 'fetch("/api", {method: "POST", body: "{}", headers: {"Content-Type": "application/json"}})'
    _page(server, "/posting", f"<script>window.done = {script}.then(() => 1)</script>")
    json = (("Content-Type", "application/json"),)
    return (
        Step("/posting", actions=(Evaluate("window.done"),)),
        Step("/api", method="POST", body=b"{}", headers=json, referrer="/posting", by_page=True),
    )


def _load(tag: str, path: str, kind: str, content: tuple[str, bytes]) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        server.routes[path] = content
        _page(server, "/loading", tag)
        dest = (("Sec-Fetch-Dest", kind),)
        return (Step("/loading"), Step(path, headers=dest, referrer="/loading", by_page=True))

    return case


CASES: dict[str, Case] = {
    "a link to the same origin": _link("localhost"),
    "a link to another site": _link(OTHER_SITE),
    "an iframe of the same origin": _iframe("localhost"),
    "an iframe of another site": _iframe(OTHER_SITE),
    "a fetch to the same origin": _fetch("localhost"),
    "a fetch to another site": _fetch(OTHER_SITE),
    "a style sheet": _load('<link rel="stylesheet" href="/style.css">', "/style.css", "style", ("text/css", b"p {}")),
    "a script": _load('<script src="/script.js"></script>', "/script.js", "script", ("text/javascript", b"0;")),
    "an image": _load(
        '<img src="/image.png" width="200" height="200" style="margin-top:4000px">',
        "/image.png",
        "image",
        ("image/png", PNG),
    ),
    "cleartext to a loopback host": _fetch("localhost", scheme="http"),
}
"""The kinds of request a page makes, by what they show."""


@pytest.mark.parametrize("case", CASES.values(), ids=CASES.keys())
async def test_the_http_fetcher_sends_what_a_page_makes_the_browser_send(server: WireServer, case: Case) -> None:
    http, browser = await run_both(server, case(server))

    assert_one_client(http, browser)


BODIES: dict[str, Case] = {"a form submission": _form, "a POST of JSON, a fetch by its body": _json_post}
"""The kinds of request with a body, whose `content-length` curl's HPACK encoder represents otherwise than Chrome's."""


@pytest.mark.parametrize("case", BODIES.values(), ids=BODIES.keys())
async def test_requests_with_a_body_send_what_the_browser_sends_but_for_their_hpack(
    server: WireServer, case: Case
) -> None:
    http, browser = await run_both(server, case(server))

    assert_one_client(http, browser, leaving_out=("HPACK",))


@pytest.mark.xfail(
    reason="curl's HPACK encoder never adds content-length to the dynamic table, which Chrome's does",
    strict=True,
)
async def test_requests_with_a_body_encode_their_headers_as_the_browser_does(server: WireServer) -> None:
    http, browser = await run_both(server, _form(server))

    assert_one_client(http, browser)


async def test_http1_names_headers_as_chrome_does(ca: trustme.CA) -> None:
    with WireServer(ca, protocols=("http/1.1",)) as server:
        http, browser = await run_both(server, _fetch("localhost")(server))

    assert_one_client(http, browser)
    [connection] = http.connections
    assert [name for name, _ in connection.requests[1].headers][:4] == [
        "Host",
        "Connection",
        "sec-ch-ua-platform",
        "User-Agent",
    ]


@pytest.mark.pernix_network  # the browser and curl map a host name that is no loopback one to the server
async def test_an_insecure_origin_gets_chromes_reduced_set(server: WireServer, monkeypatch: pytest.MonkeyPatch) -> None:
    mapped = [f"{INSECURE}:{server.port}:127.0.0.1"]
    monkeypatch.setattr(http_fetcher, "_resolve_entries", lambda _pinned: mapped)
    launch = {
        "args": [f"--host-resolver-rules=MAP {INSECURE} 127.0.0.1"],
        "proxy": {"server": "http://127.0.0.1:9", "bypass": f"<-loopback>,{INSECURE}"},  # nothing else leaves
    }

    http, browser = await run_both(
        server, _fetch(INSECURE, scheme="http", page_host=INSECURE)(server), launch_options=launch
    )

    assert_one_client(http, browser)
    [connection] = http.connections
    assert ("Accept-Encoding", "gzip, deflate") in connection.requests[1].headers
    assert not [name for name, _ in connection.requests[1].headers if name.lower().startswith("sec-")]


@pytest.mark.xfail(
    reason="Chrome orders the headers a page sets among its own by a hash of their names, which pernix does not follow",
    strict=True,
)
async def test_headers_of_the_requests_own_go_where_chrome_puts_them(server: WireServer) -> None:
    server.routes["/api"] = ("application/json", b"{}")
    script = 'fetch("/api", {headers: {"X-Custom": "a", "Authorization": "Bearer b"}})'
    _page(server, "/custom", f"<script>window.done = {script}.then(() => 1)</script>")
    own = (("Sec-Fetch-Dest", "empty"), ("X-Custom", "a"), ("Authorization", "Bearer b"))
    steps = (
        Step("/custom", actions=(Evaluate("window.done"),)),
        Step("/api", headers=own, referrer="/custom", by_page=True),
    )

    http, browser = await run_both(server, steps)

    assert_one_client(http, browser)
