"""The Profile contract for client hints and cookies: what an origin asked for, and a Session's cookies, go out alike.

An origin asks for client hints with `Accept-CH`, which each Fetcher keeps in a
Session of its own, so that what the HTTP Fetcher kept does not reach the browser;
the requests after it send them as the browser does. The cookies of a Session go
out as the browser sends them over HTTP/2, as a `cookie` field each, longer paths
first, and in one `Cookie` header over HTTP/1.1.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

import curl_cffi.aio
import pytest
import trustme

from pernix import Cookie, Session
from pernix.browser import Click, Evaluate
from profile_contract import Step, assert_one_client, run_both
from wire_server import HOST, Answer, WireServer

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

pytestmark = pytest.mark.usefixtures("browser_engine")

OTHER_SITE = "127.0.0.1"
"""The server under another name, which is another site than `localhost`."""

ASKED = (
    "Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Full-Version, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Model, "
    "Sec-CH-UA-Platform-Version, Sec-CH-UA-WoW64, Sec-CH-UA-Form-Factors, Sec-CH-Prefers-Color-Scheme, "
    "Sec-CH-Prefers-Reduced-Motion, Sec-CH-Prefers-Reduced-Transparency, Sec-CH-Viewport-Width, "
    "Sec-CH-Viewport-Height, Sec-CH-DPR, Sec-CH-Device-Memory, Device-Memory, DPR, Viewport-Width"
)
"""Every hint Chrome adds to a navigation but those that tell of the network, which follow Chrome's estimate."""

NETWORK = ("RTT", "Downlink", "ECT")
"""The hints that tell of the network, whose values follow Chrome's estimate of it, with noise for each host."""

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


def _page(body: str = "", *headers: tuple[str, str]) -> Answer:
    return Answer(body=f'<!doctype html><link rel="icon" href="data:,">{body}'.encode(), headers=headers)


def _after_asking(server: WireServer) -> tuple[Step, ...]:
    server.routes["/asking"] = _page("", ("Accept-CH", ASKED))
    return (Step("/asking"), Step("/after"))


def _form_after_asking(server: WireServer) -> tuple[Step, ...]:
    server.routes["/asking"] = _page("", ("Accept-CH", ASKED))
    server.routes["/form"] = _page(
        '<form method="post" action="/submit"><input name="a" value="1"><button id="go">go</button></form>'
    )
    form = (("Content-Type", "application/x-www-form-urlencoded"),)
    return (
        Step("/asking"),
        Step("/form", actions=(Click("#go"),)),
        Step("/submit", method="POST", body=b"a=1", headers=form, referrer="/form", by_page=True),
    )


def _iframe_after_asking(host: str) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        server.routes["/asking"] = _page("", ("Accept-CH", ASKED))
        server.routes["/frames"] = _page(f'<iframe src="{server.url("/frame", host=host)}"></iframe>')
        dest = (("Sec-Fetch-Dest", "iframe"),)
        return (
            Step("/asking"),
            Step("/frames"),
            Step("/frame", host=host, headers=dest, referrer="/frames", by_page=True),
        )

    return case


def _link_after_asking(host: str) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        server.routes["/asking"] = _page("", ("Accept-CH", ASKED))
        server.routes["/links"] = _page(f'<a id="go" href="{server.url("/target", host=host)}">go</a>')
        return (
            Step("/asking"),
            Step("/links", actions=(Click("#go"),)),
            Step("/target", host=host, referrer="/links", by_page=True),
        )

    return case


def _fetch_after_asking(host: str) -> Case:
    def case(server: WireServer) -> tuple[Step, ...]:
        server.routes["/asking"] = _page("", ("Accept-CH", ASKED))
        server.routes["/api"] = Answer("application/json", b"{}", (("Access-Control-Allow-Origin", "*"),))
        target = server.url("/api", host=host)
        server.routes["/fetching"] = _page(f'<script>window.done = fetch("{target}").then(() => 1, () => 0)</script>')
        return (
            Step("/asking"),
            Step("/fetching", actions=(Evaluate("window.done"),)),
            Step("/api", host=host, headers=(("Sec-Fetch-Dest", "empty"),), referrer="/fetching", by_page=True),
        )

    return case


def _critical(server: WireServer) -> tuple[Step, ...]:
    asks = ("Accept-CH", "Sec-CH-UA-Arch, Sec-CH-UA-Model")
    server.routes["/critical"] = _page("", asks, ("Critical-CH", "Sec-CH-UA-Arch"))
    return (Step("/critical"), Step("/after"))


def _critical_after_a_redirect(server: WireServer) -> tuple[Step, ...]:
    server.routes["/going"] = Answer(body=b"", headers=(("Location", "/critical"),), status=302)
    server.routes["/critical"] = _page("", ("Accept-CH", "Sec-CH-UA-Arch"), ("Critical-CH", "Sec-CH-UA-Arch"))
    return (Step("/going"),)


def _redirect_asking(server: WireServer) -> tuple[Step, ...]:
    asks = ("Accept-CH", "Sec-CH-UA-Arch, Sec-CH-UA-Bitness")
    server.routes["/going"] = Answer(body=b"", headers=(("Location", "/landing"), asks), status=302)
    return (Step("/going"), Step("/after"))


def _redirect_to_another_site(server: WireServer) -> tuple[Step, ...]:
    server.routes["/asking"] = _page("", ("Accept-CH", "Sec-CH-UA-Arch"))
    elsewhere = server.url("/landing", host=OTHER_SITE)
    server.routes["/going"] = Answer(body=b"", headers=(("Location", elsewhere),), status=302)
    return (Step("/asking"), Step("/going"))


def _forgetting(server: WireServer) -> tuple[Step, ...]:
    server.routes["/asking"] = _page("", ("Accept-CH", "Sec-CH-UA-Arch"))
    server.routes["/forgetting"] = _page("", ("Accept-CH", ""))
    return (Step("/asking"), Step("/forgetting"), Step("/after"))


CASES: dict[str, tuple[Case, tuple[str, bool, int]]] = {
    "a navigation after Accept-CH": (_after_asking, ("/after", True, 1)),
    "an iframe of the same origin after Accept-CH": (_iframe_after_asking(HOST), ("/frame", True, 1)),
    "an iframe of another site after Accept-CH": (_iframe_after_asking(OTHER_SITE), ("/frame", False, 1)),
    "a link to another site after Accept-CH": (_link_after_asking(OTHER_SITE), ("/target", False, 1)),
    "a fetch to another site after Accept-CH": (_fetch_after_asking(OTHER_SITE), ("/api", False, 1)),
    "a navigation sent again for Critical-CH": (_critical, ("/critical", True, 2)),
    "a redirect sent again for Critical-CH": (_critical_after_a_redirect, ("/going", True, 2)),
    "a redirect asking for hints": (_redirect_asking, ("/landing", True, 1)),
    "a redirect to another site, keeping the hints": (_redirect_to_another_site, ("/landing", True, 1)),
    "an empty Accept-CH, forgetting the hints": (_forgetting, ("/after", False, 1)),
}
"""The requests after an origin asked for client hints, by what they show, with the request that shows it:
its path, whether its last one sends `sec-ch-ua-arch`, and how many go there."""


@pytest.mark.parametrize(("case", "shown"), CASES.values(), ids=CASES.keys())
async def test_the_hints_an_origin_asked_for_go_out_as_the_browser_sends_them(
    server: WireServer, case: Case, shown: tuple[str, bool, int]
) -> None:
    http, browser = await run_both(server, case(server), new_session=Session)

    assert_one_client(http, browser)
    path, sends, times = shown
    there = [request for _, request in http.requests() if request.path == path]
    assert len(there) == times
    assert any(name == "sec-ch-ua-arch" for name, _ in there[-1].headers) == sends


async def test_a_form_submission_after_accept_ch_sends_what_the_browser_sends_but_for_its_hpack(
    server: WireServer,
) -> None:
    http, browser = await run_both(server, _form_after_asking(server), new_session=Session)

    assert_one_client(http, browser, leaving_out=("HPACK",))


async def test_the_network_hints_go_where_the_browser_puts_them(server: WireServer) -> None:
    server.routes["/asking"] = _page("", ("Accept-CH", ", ".join(NETWORK)))

    http, browser = await run_both(server, (Step("/asking"), Step("/after")), new_session=Session)

    assert_one_client(http, browser, leaving_out=("HPACK",), values_left_out=("rtt", "downlink", "ect"))
    [*_, after] = http.requests()
    assert [name for name, _ in after[1].headers if name in ("rtt", "downlink", "ect")] == ["rtt", "downlink", "ect"]


async def test_the_hints_a_session_keeps_go_out_from_its_first_request(server: WireServer) -> None:
    origin = server.url("").rstrip("/")

    def keeping() -> Session:
        return Session(client_hints={origin: ("sec-ch-ua-arch", "sec-ch-ua-model")})

    http, browser = await run_both(server, (Step("/first"),), new_session=keeping)

    assert_one_client(http, browser)
    [(_, first)] = browser.requests()
    assert ("sec-ch-ua-arch", '"x86"') in first.headers


@pytest.mark.xfail(
    reason="Chrome orders the client hints a page's requests send by a hash of their names, which pernix does not follow",
    strict=True,
)
async def test_a_fetch_to_the_same_origin_after_accept_ch_sends_its_hints_where_chrome_puts_them(
    server: WireServer,
) -> None:
    http, browser = await run_both(server, _fetch_after_asking(HOST)(server), new_session=Session)

    assert_one_client(http, browser)


def _cookies(value: str) -> Callable[[], Session]:
    """A Session with cookies of several paths, set one after another, each with a value."""

    def session() -> Session:
        now = time.time()
        paths = (("a", "/"), ("b", "/c/d"), ("c", "/c"), ("d", "/"), ("e", "/c/d"))
        cookies = [
            Cookie(name=name, value=value, domain=HOST, path=path, created=now - 50 + step, modified=now - 50 + step)
            for step, (name, path) in enumerate(paths)
        ]
        return Session(cookies=cookies)

    return session


COOKIE_STEPS = (Step("/c/d/page"), Step("/c/other"))
"""A request every cookie goes with, longer paths first, then one only those of `/` go with."""

LONG = "v" * 24
"""A cookie value long enough for curl's HPACK encoder to add its field to the dynamic table, as Chrome's does."""


async def test_the_cookies_of_a_session_go_out_as_the_browser_sends_them(server: WireServer) -> None:
    http, browser = await run_both(server, COOKIE_STEPS, new_session=_cookies(LONG))

    assert_one_client(http, browser)
    [(_, first), _] = http.requests()
    assert [value for name, value in first.headers if name == "cookie"] == [f"{name}={LONG}" for name in "becad"]


async def test_the_cookies_of_a_session_go_in_one_header_over_http1(ca: trustme.CA) -> None:
    with WireServer(ca, protocols=("http/1.1",)) as server:
        http, browser = await run_both(server, COOKIE_STEPS, new_session=_cookies("1"))

    assert_one_client(http, browser)


async def test_short_cookies_go_out_as_the_browser_sends_them_but_for_their_hpack(server: WireServer) -> None:
    http, browser = await run_both(server, COOKIE_STEPS, new_session=_cookies("1"))

    assert_one_client(http, browser, leaving_out=("HPACK",))


@pytest.mark.xfail(
    reason="curl's HPACK encoder never indexes a cookie field shorter than 20 bytes, which Chrome's adds to the "
    "dynamic table",
    strict=True,
)
async def test_short_cookies_encode_their_fields_as_the_browser_does(server: WireServer) -> None:
    http, browser = await run_both(server, COOKIE_STEPS, new_session=_cookies("1"))

    assert_one_client(http, browser)
