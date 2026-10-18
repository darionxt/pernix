from __future__ import annotations

import asyncio
import socket
import time
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import ConnectError, FetchError, NetworkBlocked, Timeout
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.launched_browser import LOOPBACK_BYPASS
from pernix.fetchers.network_guard import blocking_network
from pernix.fetchers.refusing_proxy import RefusingProxy
from pernix.response import Redirect
from pernix.testing import ReceivedRequest, Reply

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

PUBLIC = "http://192.0.2.1/"  # TEST-NET-1: never routed, so a broken guard times out rather than reaches a host

SCRIPTED = """<html><head><title>Shop</title></head><body><ul id="books"></ul>
<script>
for (const title of ["Dune", "Emma"]) {
  document.getElementById("books").insertAdjacentHTML("beforeend", `<li>${title}</li>`);
}
</script></body></html>"""


def _render(url: str, **kwargs: object) -> Request:
    return Request(url, render=True, **kwargs)  # pyright: ignore[reportArgumentType]


def _cookie_echo(request: ReceivedRequest) -> Reply:
    return Reply(f"<p>{request.header('Cookie') or ''}</p>")


async def test_the_body_is_the_page_its_scripts_rendered(server: LoopbackServer) -> None:
    server.route("/", SCRIPTED, headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/")))

    assert response.css("#books li::text").getall() == ["Dune", "Emma"]
    assert (response.status, response.url, response.fetcher) == (200, server.url("/"), "browser")
    assert response.http_version is None
    assert response.ip_address == "127.0.0.1"
    assert response.elapsed is not None
    assert response.elapsed > 0


async def test_the_rendered_page_is_utf8_whatever_the_document_declares(server: LoopbackServer) -> None:
    page = "<html><head><meta charset='gbk'></head><body><p>你好</p></body></html>".encode("gbk")
    server.route("/", page, headers=[("Content-Type", "text/html; charset=gbk")])

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/")))

    assert response.css("p::text").get() == "你好"
    assert response.encoding == "utf-8"
    assert response.headers.get("Content-Type") == "text/html; charset=gbk"
    assert "Content-Length" not in response.headers  # it describes the body sent, not the rendered one


async def test_the_response_describes_the_document_and_its_redirects(server: LoopbackServer) -> None:
    server.route("/old", status=302, headers=[("Location", "/new"), ("Set-Cookie", "hop=1")])
    server.route(
        "/new",
        "<p>new</p>",
        headers=[
            ("Content-Type", "text/html"),
            ("Set-Cookie", "a=1"),
            ("Set-Cookie", "b=2"),
            ("X-Tag", "1"),
            ("X-Tag", "2"),
        ],
    )

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/old"), cookies={"sent": "yes"}))

    assert response.url == server.url("/new")
    assert response.headers.get_all("X-Tag") == ["1", "2"]
    assert response.headers.get_all("Set-Cookie") == ["a=1", "b=2"]
    assert len(response.history) == 1
    redirect = response.history[0]
    assert isinstance(redirect, Redirect)
    assert (redirect.url, redirect.status, redirect.headers.get("Location")) == (server.url("/old"), 302, "/new")
    assert dict(response.cookies) == {"sent": "yes", "hop": "1", "a": "1", "b": "2"}


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("application/json", b'{"title": "Dune"}'),
        ("text/plain", b"plain <b>text</b>"),
        ("image/png", b"\x89PNG\r\n\x1a\n-not-really"),
    ],
)
async def test_other_documents_keep_the_body_received(server: LoopbackServer, content_type: str, body: bytes) -> None:
    server.route("/doc", body, headers=[("Content-Type", content_type)])

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/doc")))

    assert response.body == body  # not the viewer page Chrome shows it in
    assert response.headers.get("Content-Length") == str(len(body))


async def test_an_error_status_is_a_response(server: LoopbackServer) -> None:
    server.route("/gone", "<h1>Gone</h1>", status=410, headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/gone")))

    assert (response.status, response.css("h1::text").get()) == (410, "Gone")


async def test_a_response_without_content_has_an_empty_body(server: LoopbackServer) -> None:
    server.route("/empty", status=204)

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/empty")))

    assert (response.status, response.body) == (204, b"")


async def test_a_document_the_browser_downloads_fails(server: LoopbackServer) -> None:
    server.route("/file", b"\x00\x01", headers=[("Content-Type", "application/octet-stream")])

    async with BrowserFetcher() as fetcher:
        with pytest.raises(FetchError, match="fetch it without render"):
            await fetcher.fetch(_render(server.url("/file")))


async def test_the_method_body_and_headers_go_with_the_first_request(server: LoopbackServer) -> None:
    server.route("/search", status=303, headers=[("Location", "/results")], method="POST")
    server.route("/results", "<p>found</p>", headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(
            _render(server.url("/search"), data={"q": "dune"}, headers={"X-Token": "secret"})
        )

    assert response.url == server.url("/results")
    search, results = server.received
    assert (search.method, search.body) == ("POST", b"q=dune")
    assert search.header("Content-Type") == "application/x-www-form-urlencoded"
    assert ("X-Token", "secret") in search.headers  # the case given
    assert search.header("sec-ch-ua") is not None  # with the browser's own headers
    assert (results.method, results.body, results.header("Content-Type")) == ("GET", b"", None)
    assert results.header("X-Token") == "secret"  # the headers stay on redirects, as over HTTP


@pytest.mark.parametrize("name", ["Authorization", "proxy-authorization"])
async def test_credentials_in_headers_are_refused(server: LoopbackServer, name: str) -> None:
    async with BrowserFetcher() as fetcher:
        with pytest.raises(ValueError, match=f"has a {name} header, which the browser would send on to any host"):
            await fetcher.fetch(_render(server.url("/"), headers={name: "Bearer secret"}))
        launched = fetcher._launch  # pyright: ignore[reportPrivateUsage]

    assert server.received == []
    assert launched is None


@pytest.mark.parametrize(("host", "other"), [("127.0.0.1", "localhost"), ("localhost", "127.0.0.1")])
async def test_request_cookies_go_to_its_host_in_the_order_given(server: LoopbackServer, host: str, other: str) -> None:
    server.route("/same", status=302, headers=[("Location", "/echo")])
    server.route("/away", status=302, headers=[("Location", server.url("/echo").replace("127.0.0.1", other))])
    server.route("/echo", handler=_cookie_echo)
    cookies = {"a": "1", "b": "22", "c": "333"}

    async with BrowserFetcher() as fetcher:
        same = await fetcher.fetch(_render(server.url("/same").replace("127.0.0.1", host), cookies=cookies))
        away = await fetcher.fetch(_render(server.url("/away").replace("127.0.0.1", host), cookies=cookies))

    assert same.css("p::text").get() == "a=1; b=22; c=333"
    assert away.css("p::text").get() is None
    assert [r.header("Cookie") for r in server.received] == [
        "a=1; b=22; c=333",
        "a=1; b=22; c=333",
        "a=1; b=22; c=333",
        None,  # another host
    ]


async def test_prefixed_request_cookies_are_held_as_secure_ones(server: LoopbackServer) -> None:
    server.route("/echo", handler=_cookie_echo)
    cookies = {"__Host-sid": "1", "__secure-pref": "22", "plain": "333"}

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(_render(server.url("/echo"), cookies=cookies))

    # the browser takes these prefixes only on Secure cookies, sent over loopback HTTP as over HTTPS
    assert response.css("p::text").get() == "__Host-sid=1; __secure-pref=22; plain=333"


async def test_rendered_requests_are_independent(server: LoopbackServer) -> None:
    server.route("/login", "<p>in</p>", headers=[("Content-Type", "text/html"), ("Set-Cookie", "auth=ok")])
    server.route("/echo", handler=_cookie_echo)

    async with BrowserFetcher() as fetcher:
        login = await fetcher.fetch(_render(server.url("/login")))
        later = await fetcher.fetch(_render(server.url("/echo")))

    assert login.cookies == {"auth": "ok"}
    assert later.css("p::text").get() is None


async def test_a_page_that_does_not_load_in_time_is_a_timeout(server: LoopbackServer) -> None:
    def slow(_request: ReceivedRequest) -> Reply:
        time.sleep(2)
        return Reply("<p>late</p>")

    server.route("/slow", handler=slow)

    async with BrowserFetcher() as fetcher:
        with pytest.raises(Timeout, match=r"did not load within 0\.3 s"):
            await fetcher.fetch(_render(server.url("/slow"), timeout=0.3))
    async with BrowserFetcher(BrowserSettings(deadline=0.3)) as fetcher:
        with pytest.raises(Timeout, match=r"longer than browser\.deadline \(0\.3 s\)"):
            await fetcher.fetch(_render(server.url("/slow")))


async def test_a_closed_port_is_a_connect_error() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    async with BrowserFetcher() as fetcher:
        with pytest.raises(ConnectError, match="ERR_CONNECTION_REFUSED"):
            await fetcher.fetch(_render(f"http://127.0.0.1:{port}/"))


async def test_a_cancelled_fetch_leaves_the_browser_usable(server: LoopbackServer) -> None:
    loop = asyncio.get_running_loop()
    arrived = asyncio.Event()

    def slow(_request: ReceivedRequest) -> Reply:
        loop.call_soon_threadsafe(arrived.set)
        time.sleep(2)
        return Reply("<p>late</p>")

    server.route("/slow", handler=slow)
    server.route("/fast", "<p>fast</p>", headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        slow_fetch = asyncio.create_task(fetcher.fetch(_render(server.url("/slow"))))
        await arrived.wait()
        slow_fetch.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow_fetch
        response = await fetcher.fetch(_render(server.url("/fast")))

    assert response.css("p::text").get() == "fast"


async def test_closing_while_patchright_starts_leaves_nothing_running(monkeypatch: pytest.MonkeyPatch) -> None:
    import patchright.async_api

    start_playwright = patchright.async_api.async_playwright
    starting = asyncio.Event()

    def watched() -> object:
        manager = start_playwright()
        start = manager.start

        async def watched_start() -> object:
            starting.set()
            return await start()

        manager.start = watched_start  # pyright: ignore[reportAttributeAccessIssue]
        return manager

    monkeypatch.setattr(patchright.async_api, "async_playwright", watched)

    async with BrowserFetcher() as fetcher:
        fetching = asyncio.create_task(fetcher.fetch(_render("http://127.0.0.1:9/")))
        await starting.wait()
        fetching.cancel()  # closing the fetcher then stops the launch while patchright starts
        with pytest.raises(asyncio.CancelledError):
            await fetching

    left = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    assert left == []  # a task left running would hang closing the event loop


async def test_fetches_share_one_browser(server: LoopbackServer) -> None:
    server.route("/", "<p>x</p>", headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        responses = await asyncio.gather(*(fetcher.fetch(_render(server.url("/"))) for _ in range(4)))
        launches = fetcher._launch  # pyright: ignore[reportPrivateUsage]

    assert [response.status for response in responses] == [200] * 4
    assert launches is not None


async def test_a_fetcher_must_be_entered() -> None:
    with pytest.raises(RuntimeError, match="async with"):
        await BrowserFetcher().fetch(_render("http://127.0.0.1/"))


# The network guard: the pytest plugin keeps it on while each test runs, so the browser
# of every test here is launched under it; `blocking_network()` records what one fetch
# refused.


async def test_requests_a_page_makes_to_other_hosts_are_refused_and_recorded(server: LoopbackServer) -> None:
    server.route(
        "/",
        f"<p>shop</p><img src='{PUBLIC}cover.png'><script src='https://pernix.invalid/app.js'></script>",
        headers=[("Content-Type", "text/html")],
    )

    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard:
            started = time.monotonic()
            response = await fetcher.fetch(_render(server.url("/")))
            await asyncio.sleep(1)  # the browser's own requests, which are not recorded, come meanwhile

    assert time.monotonic() - started < 10
    assert response.css("p::text").get() == "shop"
    assert sorted(guard.blocked) == [f"{PUBLIC}cover.png", "https://pernix.invalid/app.js"]


async def test_a_redirect_to_another_host_is_refused(server: LoopbackServer) -> None:
    server.route("/away", status=302, headers=[("Location", PUBLIC + "page")])

    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard, pytest.raises(NetworkBlocked, match=r"refused http://192\.0\.2\.1/page, on"):
            await fetcher.fetch(_render(server.url("/away")))

    assert guard.blocked == [PUBLIC + "page"]


async def test_other_hosts_are_refused_before_the_browser_starts() -> None:
    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard, pytest.raises(NetworkBlocked):
            await fetcher.fetch(_render(PUBLIC))
        launched = fetcher._launch  # pyright: ignore[reportPrivateUsage]

    assert guard.blocked == [PUBLIC]
    assert launched is None


async def test_a_socks_proxy_with_credentials_is_refused_before_the_browser_starts() -> None:
    async with BrowserFetcher() as fetcher:
        with pytest.raises(ValueError, match="SOCKS proxy with credentials") as refused:
            await fetcher.fetch(_render("http://pernix.test/", proxy="socks5://user:secret-pw@127.0.0.1:1080"))
        launched = fetcher._launch  # pyright: ignore[reportPrivateUsage]

    assert launched is None
    assert "secret-pw" not in str(refused.value)


async def test_a_loopback_proxy_takes_requests_to_any_host(server: LoopbackServer) -> None:
    server.route("/", "<p>proxied</p>", headers=[("Content-Type", "text/html")])

    async with BrowserFetcher() as fetcher:
        with blocking_network() as guard:
            response = await fetcher.fetch(_render("http://shop.example/", proxy=server.base_url))

    assert response.css("p::text").get() == "proxied"
    assert guard.blocked == []


async def test_a_browser_launched_while_the_network_was_open_is_refused_under_the_guard(
    server: LoopbackServer,
) -> None:
    server.route("/", "<p>x</p>", headers=[("Content-Type", "text/html")])
    blackhole = RefusingProxy()  # stands in for the open network, so this test reaches nothing
    await blackhole.start()
    settings = BrowserSettings(launch_options={"proxy": {"server": blackhole.url, "bypass": LOOPBACK_BYPASS}})

    try:
        async with BrowserFetcher(settings) as fetcher:
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr("pernix.fetchers.launched_browser.current_guard", lambda: None)
                patch.setattr("pernix.fetchers.browser_fetcher.current_guard", lambda: None)
                await fetcher.fetch(_render(server.url("/")))  # launches the browser as if the network were open
            with blocking_network() as guard, pytest.raises(NetworkBlocked, match="launched while the network"):
                await fetcher.fetch(_render(server.url("/")))
    finally:
        await blackhole.close()

    assert guard.blocked == [server.url("/")]
