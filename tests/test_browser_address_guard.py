"""The browser Fetcher under `network.public_only`: every host the browser reaches goes through a proxy that checks it."""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import pytest

from pernix import Request, Session
from pernix.browser import Click, Evaluate, Render
from pernix.errors import NetworkBlocked, ProxyError
from pernix.fetchers import BrowserFetcher
from pernix.fetchers.address_guard import AddressGuard
from pernix.fetchers.address_guard_proxy import AddressGuardProxy
from pernix.settings import NetworkSettings
from pernix.stats import Stats

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

GUARDED = NetworkSettings(public_only=True, allow_hosts=("localhost",))
"""Only public hosts, and localhost, which the network guard of tests admits too: the pages these tests render."""

HTML = [("Content-Type", "text/html")]


def _render(url: str, **kwargs: Any) -> Request:
    return Request(url, **{"render": True, **kwargs})


def _port(server: LoopbackServer) -> int:
    port = urlsplit(server.base_url).port
    assert port is not None
    return port


async def test_a_loopback_document_is_refused_before_the_browser_is_launched(server: LoopbackServer) -> None:
    stats = Stats()

    async with BrowserFetcher(stats=stats, network=GUARDED) as fetcher:
        with pytest.raises(NetworkBlocked) as refused:
            await fetcher.fetch(_render(server.url("/secret")))

    assert str(refused.value) == f"network.public_only refused {server.url('/secret')}: 127.0.0.1 is a loopback address"
    assert stats.get("browser.generations") is None
    assert server.received == []


async def test_a_redirect_to_a_loopback_address_is_refused(server: LoopbackServer) -> None:
    port = _port(server)
    server.route("/hop", status=302, headers=[("Location", server.url("/secret"))])
    server.route("/secret", "<p>secret</p>", headers=HTML)

    async with BrowserFetcher(network=GUARDED) as fetcher:
        with pytest.raises(NetworkBlocked) as refused:
            await fetcher.fetch(_render(f"http://localhost:{port}/hop"))

    assert str(refused.value) == (
        f"network.public_only refused the redirect from http://localhost:{port}/hop to {server.url('/secret')}: "
        "127.0.0.1 is a loopback address"
    )
    assert [request.path for request in server.received] == ["/hop"]


async def test_a_page_on_an_allowed_host_renders_without_the_loopback_requests_it_makes(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    port = _port(server)
    page = f"""<p>page</p><img src="{server.url("/pixel")}">
<script>
const socket = new WebSocket("ws://127.0.0.1:{port}/socket");
socket.onclose = () => document.body.insertAdjacentHTML("beforeend", "<p id='closed'>closed</p>");
</script>"""
    server.route("/page", page, headers=HTML)
    url = f"http://localhost:{port}/page"
    stats = Stats()
    caplog.set_level(logging.DEBUG, logger="pernix.fetchers.browser_fetcher")

    async with BrowserFetcher(stats=stats, network=GUARDED) as fetcher:
        response = await fetcher.fetch(_render(url, render=Render(wait_for="#closed")))

    assert (response.status, response.css("#closed::text").get(), response.ip_address) == (200, "closed", "127.0.0.1")
    assert [request.path for request in server.received] == ["/page"]
    refused = stats.get("browser.requests_refused")
    assert isinstance(refused, int)
    assert refused >= 1  # the image, before the page loaded; the WebSocket may be told after the fetch
    assert (
        f"network.public_only refused {server.url('/pixel')}, requested by the page of {url}: "
        "127.0.0.1 is a loopback address"
    ) in caplog.messages


async def test_a_page_navigating_to_a_loopback_address_is_refused(server: LoopbackServer) -> None:
    port = _port(server)
    server.route("/", f"<a href='{server.url('/secret')}'>next</a>", headers=HTML)

    async with BrowserFetcher(network=GUARDED) as fetcher:
        with pytest.raises(NetworkBlocked) as refused:
            await fetcher.fetch(_render(f"http://localhost:{port}/", render=Render(actions=[Click("a")])))

    assert str(refused.value) == (
        f"network.public_only refused {server.url('/secret')}, to which the page of http://localhost:{port}/ "
        "navigated: 127.0.0.1 is a loopback address"
    )
    assert [request.path for request in server.received] == ["/"]


@pytest.mark.pernix_network  # the host is no loopback name; the proxy resolves it, then reaches what it checked
async def test_a_name_answering_with_a_private_address_once_checked_cannot_reach_it(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    port = _port(server)
    server.route("/page", "<p>public</p>", headers=HTML)
    try:
        private = socket.create_server(("::1", port), family=socket.AF_INET6)  # the private address, on the same port
    except OSError as exc:
        pytest.skip(f"no IPv6 loopback address to stand for a private one: {exc}")

    def getaddrinfo(host: str, port: int, *_args: object, **_kwargs: object) -> list[object]:
        if host != "rebind.invalid":  # .invalid never resolves, so the browser cannot have resolved it itself
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
        if server.received:  # once the page was served, the name answers otherwise
            return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", port, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    monkeypatch.setattr("pernix.fetchers.address_guard.is_public_address", lambda address: str(address) == "127.0.0.1")
    url = f"http://rebind.invalid:{port}/page"

    with private:
        private.setblocking(False)
        async with BrowserFetcher(network=NetworkSettings(public_only=True)) as fetcher:
            response = await fetcher.fetch(_render(url))
            with pytest.raises(NetworkBlocked) as refused:
                await fetcher.fetch(_render(url))  # a context of its own, so a connection of its own
        with pytest.raises(BlockingIOError):
            private.accept()  # nothing connected to it

    assert (response.css("p::text").get(), response.ip_address) == ("public", "127.0.0.1")
    assert server.received[0].header("Host") == f"rebind.invalid:{port}"
    assert (
        str(refused.value) == f"network.public_only refused {url}: rebind.invalid resolves to ::1, a loopback address"
    )


async def test_requests_through_a_proxy_of_their_own_or_of_their_session_are_refused(server: LoopbackServer) -> None:
    server.route("/", "<p>direct</p>", headers=HTML)
    url = f"http://localhost:{_port(server)}/"
    proxy = "http://user:secret@127.0.0.1:3128"

    async with BrowserFetcher(network=GUARDED) as fetcher:
        with pytest.raises(NetworkBlocked) as own:
            await fetcher.fetch(_render(url, proxy=proxy))
        with pytest.raises(NetworkBlocked) as bound:
            await fetcher.fetch(_render(url), session=Session("alice", proxy=proxy))
        response = await fetcher.fetch(_render(url, proxy=""), session=Session("bob", proxy=proxy))  # none

    assert str(own.value) == (
        f"network.public_only refused {url}: the browser cannot check the hosts a page reaches through a proxy; "
        "render it without one, or fetch it over HTTP"
    )
    assert str(bound.value) == str(own.value)
    assert response.css("p::text").get() == "direct"
    assert [request.path for request in server.received] == ["/"]


@pytest.mark.parametrize("public_only", [False, True], ids=["network guard", "public_only"])
async def test_webrtc_is_kept_off_udp_which_would_go_around_the_proxy(
    server: LoopbackServer, public_only: bool
) -> None:
    server.route("/", "<p>call</p>", headers=HTML)
    stun = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stun.bind(("127.0.0.1", 0))
    stun.setblocking(False)
    gather = Evaluate(
        f"""() => new Promise(resolve => {{
  const connection = new RTCPeerConnection({{iceServers: [{{urls: "stun:127.0.0.1:{stun.getsockname()[1]}"}}]}});
  connection.createDataChannel("probe");
  const done = () => resolve(connection.iceGatheringState);
  connection.onicegatheringstatechange = () => connection.iceGatheringState === "complete" && done();
  setTimeout(done, 8000);
  connection.createOffer().then(offer => connection.setLocalDescription(offer));
}})"""
    )
    url = f"http://localhost:{_port(server)}/" if public_only else server.url("/")

    with stun:
        async with BrowserFetcher(network=GUARDED if public_only else None) as fetcher:
            response = await fetcher.fetch(_render(url, render=Render(actions=[gather])))
        with pytest.raises(BlockingIOError):
            stun.recvfrom(2048)  # no STUN request arrived

    assert response.browser is not None
    assert response.browser.evaluated == ("complete",)


async def test_a_socks_proxy_refusing_a_host_fails_the_fetch_with_a_proxy_error(server: LoopbackServer) -> None:
    server.route("/", "<p>page</p>", headers=HTML)
    proxy = AddressGuardProxy(AddressGuard())  # a SOCKS5 proxy that refuses loopback hosts
    await proxy.start()

    try:
        async with BrowserFetcher() as fetcher:
            with pytest.raises(ProxyError, match=r"net::ERR_SOCKS_CONNECTION_FAILED"):
                await fetcher.fetch(_render(server.url("/"), proxy=proxy.url))
    finally:
        await proxy.close()

    assert server.received == []
