"""The browser's own requests, which belong to no context, and so ignore the contexts' proxies: they leave nothing."""

from __future__ import annotations

import asyncio
import ipaddress
import os
from typing import TYPE_CHECKING

import psutil
import pytest

from pernix.fetchers import BrowserSettings
from pernix.fetchers.launched_browser import launch_browser
from test_profile_contract_proxy import RecordingProxy

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")


def _off_loopback() -> list[str]:
    """The connections of this process's descendants, the browser's, to addresses other than loopback ones."""
    found: list[str] = []
    for process in psutil.Process(os.getpid()).children(recursive=True):
        try:
            connections = process.net_connections(kind="inet")
        except psutil.Error:  # gone meanwhile
            continue
        found += [
            f"{process.name()} to {connection.raddr.ip}:{connection.raddr.port}"
            for connection in connections
            if connection.raddr and not ipaddress.ip_address(connection.raddr.ip).is_loopback
        ]
    return found


@pytest.mark.pernix_network  # as on an open network: the browser's own requests would leave from this machine
async def test_the_browsers_own_requests_are_refused_while_its_contexts_go_through_their_proxies(
    server: LoopbackServer,
) -> None:
    server.route("/", "<p>direct</p>", headers=[("Content-Type", "text/html")])
    session_proxy = RecordingProxy()
    await session_proxy.start()
    launched = await launch_browser(BrowserSettings())
    try:
        refusing = launched.refusing_proxy
        assert refusing is not None
        proxied = await launched.new_context(proxy=session_proxy.url())
        page = await proxied.new_page()
        with pytest.raises(Exception, match="ERR_TUNNEL_CONNECTION_FAILED"):  # the proxy opens no tunnel
            await page.goto("https://site.test/")
        for _ in range(200):  # the browser asks for the network time, and for its accounts, as it starts
            if refusing.refused:
                break
            await asyncio.sleep(0.05)
        left = _off_loopback()
        direct = await launched.new_context()  # without a proxy: it reaches hosts directly
        page = await direct.new_page()
        response = await page.goto(server.url("/"))
        assert response is not None
        loaded = (response.status, await response.text())
        await proxied.close()
        await direct.close()
    finally:
        await launched.close()
        await session_proxy.close()

    assert refusing.refused
    assert all("google" in target for target in refusing.refused), list(refusing.refused)
    assert "CONNECT site.test:443 HTTP/1.1" in [line for line, _ in session_proxy.heads]
    assert not any("google" in line and "www.google.com" not in line for line, _ in session_proxy.heads)
    assert left == []
    assert loaded == (200, "<p>direct</p>")
    assert [request.path for request in server.received] == ["/"]
