"""Render options on loopback pages: waiting, actions, blocking resources, capturing responses, and screenshots."""

from __future__ import annotations

import logging
import socket
import struct
from typing import TYPE_CHECKING

import pytest

from pernix import Request, Session
from pernix.browser import Click, Evaluate, Render, Screenshot, Scroll, Type, WaitFor
from pernix.errors import ActionError, ActionTimeout, ConnectError, NetworkBlocked, Timeout
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.network_guard import blocking_network

if TYPE_CHECKING:
    from pernix.response import Response
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]

LATER = """<html><body><p id="now">now</p>
<script>
addEventListener("load", () => setTimeout(() => document.body.insertAdjacentHTML("beforeend", "<p class='late'>late</p>"), 1000));
</script></body></html>"""

ENDLESS = """<html><body>
<script>
let shown = 0, bottom = 0;
const more = () => { for (let i = 0; i < 5; i++) document.body.insertAdjacentHTML("beforeend", `<div class="item" style="height: 400px">${shown++}</div>`); };
more();
addEventListener("scroll", () => { if (scrollY > bottom) { bottom = scrollY; more(); } });
</script></body></html>"""

FORM = """<html><head><title>Search</title></head><body>
<input id="q"><p id="echo"></p><button id="more" onclick="document.body.insertAdjacentHTML('beforeend', '<p class=added>added</p>')">More</button>
<script>
window.__DATA__ = {books: 2};
const q = document.getElementById("q");
q.addEventListener("keyup", () => { document.getElementById("echo").textContent = q.value; });
</script></body></html>"""

RESOURCES = """<html><head><link rel="stylesheet" href="/style.css"></head>
<body><img src="/cover.png"><p>Dune</p></body></html>"""

API = """<html><body><ul id="books"></ul>
<script>
fetch("/api/other").then(() => {});
fetch("/api/list?page=1").then(r => r.json()).then(data => {
  for (const title of data.books) document.getElementById("books").insertAdjacentHTML("beforeend", `<li>${title}</li>`);
});
</script></body></html>"""

PNG = b"\x89PNG\r\n\x1a\n"


def _rendered(url: str, render: Render, **kwargs: object) -> Request:
    return Request(url, render=render, **kwargs)  # pyright: ignore[reportArgumentType]


def _png_size(image: bytes) -> tuple[int, int]:
    """The width and height of a PNG, from its header."""
    assert image.startswith(PNG)
    width, height = struct.unpack(">II", image[16:24])
    return width, height


async def _fetch(server_url: str, render: Render | bool, settings: BrowserSettings | None = None) -> Response:
    async with BrowserFetcher(settings) as fetcher:
        return await fetcher.fetch(Request(server_url, render=render))


async def test_wait_for_waits_for_content_added_later(server: LoopbackServer) -> None:
    server.route("/", LATER, headers=HTML)

    async with BrowserFetcher() as fetcher:
        loaded = await fetcher.fetch(Request(server.url("/"), render=True))
        waited = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p.late")))

    assert loaded.css("p.late").get() is None
    assert waited.css("p.late::text").get() == "late"


async def test_wait_for_actions_wait_for_any_state(server: LoopbackServer) -> None:
    page = """<html><body><p id="spinner">loading</p>
<script>addEventListener("load", () => setTimeout(() => document.getElementById("spinner").remove(), 300));</script>
</body></html>"""
    server.route("/", page, headers=HTML)

    response = await _fetch(server.url("/"), Render(actions=[WaitFor("#spinner", state="detached")]))

    assert response.css("#spinner").get() is None


async def test_scroll_loads_what_the_page_adds_at_its_bottom(server: LoopbackServer) -> None:
    server.route("/", ENDLESS, headers=HTML)

    response = await _fetch(server.url("/"), Render(actions=[Scroll(times=3, delay=0.2)]))

    assert len(response.css("div.item")) == 20


async def test_click_and_type_act_on_the_page(server: LoopbackServer) -> None:
    server.route("/", FORM, headers=HTML)

    response = await _fetch(server.url("/"), Render(actions=[Type("#q", "dune", delay=0.01), Click("#more")]))

    assert response.css("#echo::text").get() == "dune"
    assert response.css("p.added::text").get() == "added"


async def test_evaluate_runs_in_the_page_unless_isolated(server: LoopbackServer) -> None:
    server.route("/", FORM, headers=HTML)
    render = Render(
        actions=[
            Evaluate("() => window.__DATA__"),
            Evaluate("() => typeof window.__DATA__", isolated=True),
            Evaluate("document.title", isolated=True),
            Evaluate("() => new Promise(resolve => setTimeout(() => resolve([1, 'two', null]), 50))"),
        ]
    )

    response = await _fetch(server.url("/"), render)

    assert response.browser is not None
    assert response.browser.evaluated == ({"books": 2}, "undefined", "Search", [1, "two", None])


async def test_screenshots_are_pngs_in_the_order_taken(server: LoopbackServer) -> None:
    server.route("/", "<html><body><div style='height: 3000px'>tall</div></body></html>", headers=HTML)
    render = Render(actions=[Screenshot(full_page=False), Screenshot()], screenshot=True)

    response = await _fetch(server.url("/"), render)

    assert response.browser is not None
    viewport, whole, last = (_png_size(image) for image in response.browser.screenshots)
    assert viewport[1] < 3000 <= whole[1]
    assert last == whole


async def test_browser_block_applies_to_every_rendered_request(server: LoopbackServer) -> None:
    server.route("/", RESOURCES, headers=HTML)
    server.route("/style.css", "p { color: red }", headers=[("Content-Type", "text/css")])
    server.route("/cover.png", PNG, headers=[("Content-Type", "image/png")])

    async with BrowserFetcher(BrowserSettings(block=("image",))) as fetcher:
        plain = await fetcher.fetch(Request(server.url("/"), render=True))
        with_options = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p")))

    assert plain.css("p::text").get() == with_options.css("p::text").get() == "Dune"
    assert [request.path for request in server.received] == ["/", "/style.css", "/", "/style.css"]


@pytest.mark.parametrize(
    ("block", "loaded"),
    [
        ((), ["/", "/style.css", "/cover.png"]),
        (("stylesheet",), ["/", "/cover.png"]),
        (None, ["/", "/style.css"]),
    ],
)
async def test_render_block_replaces_browser_block(
    server: LoopbackServer, block: tuple[str, ...] | None, loaded: list[str]
) -> None:
    server.route("/", RESOURCES, headers=HTML)
    server.route("/style.css", "p { color: red }", headers=[("Content-Type", "text/css")])
    server.route("/cover.png", PNG, headers=[("Content-Type", "image/png")])

    response = await _fetch(server.url("/"), Render(block=block), BrowserSettings(block=("image",)))  # pyright: ignore[reportArgumentType]

    assert response.status == 200
    assert sorted(request.path for request in server.received) == sorted(loaded)


async def test_blocking_keeps_the_method_body_and_headers_of_the_first_request(server: LoopbackServer) -> None:
    server.route("/", RESOURCES, headers=HTML, method="POST")
    server.route("/cover.png", PNG, headers=[("Content-Type", "image/png")])

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(
            _rendered(server.url("/"), Render(block=("image", "stylesheet")), body=b"q=dune", headers={"X-Shop": "1"})
        )

    (received,) = server.received
    assert (received.method, received.body, received.header("X-Shop")) == ("POST", b"q=dune", "1")


async def test_capture_keeps_the_matching_xhr_and_fetch_responses(server: LoopbackServer) -> None:
    server.route("/", API, headers=HTML)
    server.route("/api/list", '{"books": ["Dune", "Emma"]}', headers=[("Content-Type", "application/json")])
    server.route("/api/other", "{}", headers=[("Content-Type", "application/json")])

    response = await _fetch(server.url("/"), Render(wait_for="#books li", capture="**/api/list*"))

    assert response.css("#books li::text").getall() == ["Dune", "Emma"]
    assert response.browser is not None
    (captured,) = response.browser.captured
    assert (captured.url, captured.status, captured.body) == (
        server.url("/api/list?page=1"),
        200,
        b'{"books": ["Dune", "Emma"]}',
    )
    assert captured.headers.get("Content-Type") == "application/json"


async def test_the_document_is_kept_as_received_before_its_scripts_ran(server: LoopbackServer) -> None:
    server.route("/", LATER, headers=[*HTML, ("X-Shop", "1")])

    response = await _fetch(server.url("/"), Render(wait_for="p.late"))

    assert response.browser is not None
    document = response.browser.document
    assert document is not None
    assert (document.url, document.status, document.body) == (server.url("/"), 200, LATER.encode())
    assert document.headers.get("X-Shop") == "1"
    assert b'class="late"' in response.body
    assert response.browser.captured == response.browser.screenshots == response.browser.evaluated == ()


async def test_a_render_without_options_still_gives_the_document(server: LoopbackServer) -> None:
    server.route("/", "<p>plain</p>", headers=HTML)

    response = await _fetch(server.url("/"), True)

    assert response.browser is not None
    assert response.browser.document is not None
    assert response.browser.document.body == b"<p>plain</p>"


async def test_an_action_that_takes_too_long_is_a_timeout_naming_it(server: LoopbackServer) -> None:
    server.route("/", "<p>plain</p>", headers=HTML)
    url = server.url("/")

    with pytest.raises(ActionTimeout, match=r"actions\[1\], WaitFor\('p.never'\), on .* took longer than 0.2 s"):
        await _fetch(url, Render(actions=[Evaluate("1"), WaitFor("p.never", timeout=0.2)]))
    with pytest.raises(ActionTimeout, match=r"actions\[0\], Evaluate, on .* took longer than 0.2 s"):
        await _fetch(url, Render(actions=[Evaluate("() => new Promise(() => {})", timeout=0.2)]))
    with pytest.raises(ActionTimeout, match=r"Render\(wait_for='p.never'\) on .* took longer than 0.2 s"):
        await _fetch(url, Render(wait_for="p.never"), BrowserSettings(action_timeout=0.2))

    assert issubclass(ActionTimeout, Timeout)  # retried by the default retry policy


@pytest.mark.parametrize(
    ("action", "message"),
    [
        (Evaluate("() => { throw new Error('boom') }"), "Error: boom"),
        (Click("p[["), "while parsing css selector"),
    ],
)
async def test_an_action_that_fails_otherwise_is_an_action_error(
    server: LoopbackServer, action: Evaluate | Click, message: str
) -> None:
    server.route("/", "<p>plain</p>", headers=HTML)

    with pytest.raises(ActionError, match=message) as raised:
        await _fetch(server.url("/"), Render(actions=[action]))

    assert not isinstance(raised.value, Timeout)


async def test_an_optional_action_that_fails_is_logged_and_skipped(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/", FORM, headers=HTML)
    render = Render(
        actions=[
            Click("#cookie-banner", optional=True, timeout=0.2),
            Evaluate("() => { throw new Error('boom') }", optional=True),
            Evaluate("document.title"),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="pernix.fetchers.page_actions"):
        response = await _fetch(server.url("/"), render)

    assert response.browser is not None
    assert response.browser.evaluated == ("Search",)
    assert [record.getMessage().split(",")[0] for record in caplog.records] == ["actions[0]", "actions[1]"]
    assert all("going on, as the action is optional" in record.getMessage() for record in caplog.records)


async def test_type_types_after_what_an_element_holds(server: LoopbackServer) -> None:
    page = (
        "<input id='q' value='old'><textarea id='notes'>first\nsecond</textarea>"
        "<div id='edit' contenteditable>kept</div>"
    )
    server.route("/", page, headers=HTML)
    read = Evaluate("() => ['q', 'notes'].map(id => document.getElementById(id).value).concat(edit.textContent)")

    response = await _fetch(
        server.url("/"), Render(actions=[Type("#q", "new"), Type("#notes", "!"), Type("#edit", "+"), read])
    )

    assert response.browser is not None
    assert response.browser.evaluated == (["oldnew", "first\nsecond!", "kept+"],)


async def test_typed_text_stays_out_of_errors_logs_and_reprs(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/", FORM, headers=HTML)
    typed = Type("#password", "hunter2", timeout=0.2)

    with pytest.raises(ActionTimeout) as raised:
        await _fetch(server.url("/"), Render(actions=[typed]))
    with caplog.at_level(logging.WARNING, logger="pernix.fetchers.page_actions"):
        await _fetch(server.url("/"), Render(actions=[Type("#password", "hunter2", timeout=0.2, optional=True)]))

    assert "actions[0], Type('#password'), on" in str(raised.value)
    assert [record.getMessage().split(" on ")[0] for record in caplog.records] == ["actions[0], Type('#password'),"]
    assert "hunter2" not in f"{raised.value} {caplog.text} {typed!r} {Render(actions=[typed])!r}"
    assert repr(typed) == "Type(selector='#password', text='***')"


async def test_actions_that_navigate_give_the_document_they_lead_to(server: LoopbackServer) -> None:
    server.route("/", "<a href='/gone'>next</a>", headers=HTML)
    server.route("/gone", "<p class='gone'>gone</p>", status=404, headers=[*HTML, ("X-Shop", "gone")])

    response = await _fetch(server.url("/"), Render(actions=[Click("a"), WaitFor("p.gone")]))

    assert (response.status, response.url, response.headers.get("X-Shop")) == (404, server.url("/gone"), "gone")
    assert response.css("p.gone::text").get() == "gone"
    assert response.browser is not None
    assert response.browser.document is not None
    assert (response.browser.document.url, response.browser.document.status) == (server.url("/gone"), 404)
    assert response.browser.document.body == b"<p class='gone'>gone</p>"


@pytest.mark.parametrize("status", [204, 205])
async def test_an_action_whose_navigation_shows_no_document_keeps_the_page(server: LoopbackServer, status: int) -> None:
    page = "<form action='/send' method='post'><button id='send'>Send</button></form><p id='kept'>kept</p>"
    server.route("/", page, headers=[*HTML, ("X-Shop", "page")])
    server.route("/send", b"", status=status, method="POST")
    settle = Evaluate("() => new Promise(resolve => setTimeout(resolve, 300))")  # the answer arrives meanwhile

    response = await _fetch(server.url("/"), Render(actions=[Click("#send"), settle]))

    assert [request.path for request in server.received] == ["/", "/send"]
    assert (response.status, response.url, response.headers.get("X-Shop")) == (200, server.url("/"), "page")
    assert response.css("#kept::text").get() == "kept"
    assert response.browser is not None
    assert response.browser.document is not None
    assert (response.browser.document.status, response.browser.document.body) == (200, page.encode())


async def test_an_action_leading_to_a_page_that_does_not_load_fails(server: LoopbackServer) -> None:
    with socket.create_server(("127.0.0.1", 0)) as listening:
        closed = f"http://127.0.0.1:{listening.getsockname()[1]}/next"
    server.route("/", f"<a href='{closed}'>next</a>", headers=HTML)

    with pytest.raises(
        ConnectError,
        match=r"the page of .* navigated to http://127\.0\.0\.1:\d+/next, which did not load: net::ERR_CONN",
    ):
        await _fetch(server.url("/"), Render(actions=[Click("a")]))


async def test_an_action_leading_to_a_refused_host_is_blocked(server: LoopbackServer) -> None:
    server.route("/", "<a href='http://192.0.2.1/next'>next</a>", headers=HTML)

    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard, pytest.raises(NetworkBlocked, match=r"refused http://192\.0\.2\.1/next, on"):
            await fetcher.fetch(_rendered(server.url("/"), Render(actions=[Click("a")])))

    assert guard.blocked == ["http://192.0.2.1/next"]


SERVICE_WORKER = """self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", event => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", event => event.respondWith(fetch(event.request)));"""

WORKER_PAGE = """<html><body><script>
const show = text => document.body.insertAdjacentHTML("beforeend", `<img src="/cover.png"><p class=ready>${text}</p>`);
if (navigator.serviceWorker.controller) {
  show("worker");
} else {
  navigator.serviceWorker.register("/worker.js").then(() => navigator.serviceWorker.ready).then(() => {
    const controlled = () => document.querySelector("p.ready") || show("worker");
    if (navigator.serviceWorker.controller) controlled();
    else navigator.serviceWorker.addEventListener("controllerchange", controlled);
  });
  setTimeout(() => document.querySelector("p.ready") || show("no worker"), 2000);  // registering never ends if blocked
}
</script></body></html>"""


def _serve_worker(server: LoopbackServer) -> None:
    """A page whose image a service worker loads once it controls the page, and the page itself otherwise."""
    server.route("/", WORKER_PAGE, headers=HTML)
    server.route("/worker.js", SERVICE_WORKER, headers=[("Content-Type", "text/javascript")])
    server.route("/cover.png", PNG, headers=[("Content-Type", "image/png")])


async def test_service_workers_cannot_load_what_is_blocked(server: LoopbackServer) -> None:
    _serve_worker(server)

    async with BrowserFetcher() as fetcher:
        open_page = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p.ready")))
        blocking = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p.ready", block="image")))

    assert open_page.css("p.ready::text").get() == "worker"  # the worker would have loaded the image
    assert blocking.css("p.ready::text").get() == "no worker"
    assert [request.path for request in server.received].count("/cover.png") == 1


async def test_service_workers_of_a_session_cannot_load_what_is_blocked(server: LoopbackServer) -> None:
    _serve_worker(server)
    session = Session("alice")

    async with BrowserFetcher() as fetcher:
        registered = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p.ready")), session=session)
        blocking = await fetcher.fetch(
            _rendered(server.url("/"), Render(wait_for="p.ready", block="image")), session=session
        )
        again = await fetcher.fetch(_rendered(server.url("/"), Render(wait_for="p.ready")), session=session)

    assert registered.css("p.ready::text").get() == "worker"
    assert blocking.css("p.ready::text").get() == "no worker"
    assert again.css("p.ready::text").get() == "worker"
    assert [request.path for request in server.received].count("/cover.png") == 2  # the first fetch and the last


async def test_the_browser_stays_usable_after_an_action_failed(server: LoopbackServer) -> None:
    server.route("/", "<p>plain</p>", headers=HTML)

    async with BrowserFetcher() as fetcher:
        with pytest.raises(ActionTimeout):
            await fetcher.fetch(_rendered(server.url("/"), Render(actions=[WaitFor("p.never", timeout=0.2)])))
        response = await fetcher.fetch(Request(server.url("/"), render=True))

    assert response.css("p::text").get() == "plain"


async def test_actions_and_blocking_apply_to_one_fetch_of_a_session(server: LoopbackServer) -> None:
    server.route("/", RESOURCES, headers=HTML)
    server.route("/style.css", "p { color: red }", headers=[("Content-Type", "text/css")])
    server.route("/cover.png", PNG, headers=[("Content-Type", "image/png")])
    session = Session("alice")

    async with BrowserFetcher() as fetcher:
        blocked = await fetcher.fetch(
            _rendered(server.url("/"), Render(block=("image",), actions=[Evaluate("document.images.length")])),
            session=session,
        )
        later = await fetcher.fetch(Request(server.url("/"), render=True), session=session)

    assert blocked.browser is not None
    assert blocked.browser.evaluated == (1,)
    assert later.browser is not None
    assert later.browser.evaluated == ()
    assert [request.path for request in server.received].count("/cover.png") == 1  # the later fetch blocked nothing
