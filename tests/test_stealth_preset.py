"""The stealth preset, the system's own Chrome with a window, and WebRTC kept off UDP with a Proxy Pool."""

from __future__ import annotations

import logging
import socket
from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Spider
from pernix.browser import Evaluate, Render
from pernix.crawler import Crawler
from pernix.errors import ComponentError, SettingsError
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.launched_browser import launch_browser, probe_browser, report_unpinned
from pernix.fetchers.pinned_pair import BUNDLED_BROWSER
from pernix.settings import CORE_SECTIONS, DEFAULT_LOCALE, load_settings

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.testing import LoopbackServer

HTML = [("Content-Type", "text/html")]


class _Idle(Spider):
    pass


def _settings(tmp_path: Path, **overrides: Any) -> BrowserSettings:
    return load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ={}, overrides=overrides).section(
        BrowserSettings
    )


def _crawler(tmp_path: Path, **overrides: Any) -> Crawler:
    settings = load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ={}, overrides=overrides)
    return Crawler(_Idle(), settings=settings)


def test_the_preset_tells_whether_the_browser_runs_without_a_window_unless_headless_does(tmp_path: Path) -> None:
    assert (_settings(tmp_path).preset, _settings(tmp_path).runs_headless) == ("bundled", True)
    assert _settings(tmp_path, **{"browser.preset": "stealth"}).runs_headless is False
    assert _settings(tmp_path, **{"browser.preset": "stealth", "browser.headless": True}).runs_headless is True
    assert _settings(tmp_path, **{"browser.headless": False}).runs_headless is False
    with pytest.raises(SettingsError, match=r"browser\.preset"):
        _settings(tmp_path, **{"browser.preset": "headed"})


async def test_a_browser_that_is_not_installed_is_named_with_how_to_install_it() -> None:
    settings = BrowserSettings(launch_options={"channel": "msedge-canary"})  # installed nowhere it is tested

    with pytest.raises(ComponentError) as missing:
        await launch_browser(settings)

    assert "distribution 'msedge-canary' is not found at " in str(missing.value)
    assert str(missing.value).endswith("; install it, such as with: patchright install msedge-canary")


@pytest.mark.usefixtures("browser_engine")
async def test_the_stealth_preset_launches_the_systems_chrome_with_a_window_and_pages_without_a_viewport(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/", "<p>windowed</p>", headers=HTML)
    settings = BrowserSettings(preset="stealth")
    try:
        launched = await launch_browser(settings)
    except ComponentError as exc:  # no system Chrome, or no display, as on a server
        if " is not found at " in str(exc) or "needs a display" in str(exc):
            pytest.skip(str(exc))
        raise
    try:
        context = await launched.new_context()
        page = await launched.new_page(context)
        await page.goto(server.url("/"))
        brands = await page.evaluate("navigator.userAgentData.brands.map(brand => brand.brand)")
        agent = await page.evaluate("navigator.userAgent")
        viewport = page.viewport_size
        version = launched.browser.version
        with caplog.at_level(logging.INFO, logger="pernix.fetchers.launched_browser"):
            await report_unpinned(launched, DEFAULT_LOCALE)
    finally:
        await launched.close()

    assert "Google Chrome" in brands  # Chrome for Testing names the Chromium brand alone
    assert "HeadlessChrome" not in agent
    assert viewport is None
    if version != BUNDLED_BROWSER.version:
        assert [(record.levelno, record.getMessage()) for record in caplog.records] == [
            (
                logging.INFO,
                f"The stealth preset launched the browser {version}, while the HTTP Fetcher presents the bundled "
                f"build {BUNDLED_BROWSER.version} with the pinned Profile, so the Sessions both use present two "
                "clients",
            )
        ]
        assert (await probe_browser(settings)).endswith(
            f" {version} (stealth preset; the HTTP Fetcher presents the bundled {BUNDLED_BROWSER.version})"
        )


def test_a_run_naming_its_browser_is_not_warned_about_under_the_stealth_preset(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    channel = {"browser.launch_options": {"channel": "chrome-beta"}}

    with caplog.at_level(logging.WARNING, logger="pernix.fetchers"):
        BrowserFetcher.from_crawler(_crawler(tmp_path, **channel, **{"browser.preset": "stealth"}))
        quiet = list(caplog.records)
        BrowserFetcher.from_crawler(_crawler(tmp_path, **channel))

    assert quiet == []
    assert caplog.records[0].getMessage().startswith("browser.launch_options names another browser")


def test_a_run_with_a_proxy_pool_keeps_webrtc_off_udp(tmp_path: Path) -> None:
    pooled = BrowserFetcher.from_crawler(_crawler(tmp_path, **{"proxies.urls": ["http://127.0.0.1:3128"]}))
    direct = BrowserFetcher.from_crawler(_crawler(tmp_path))

    assert pooled._proxy_pool  # pyright: ignore[reportPrivateUsage]
    assert not direct._proxy_pool  # pyright: ignore[reportPrivateUsage]


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.pernix_network  # as on an open network, where only a Proxy Pool keeps WebRTC off UDP
async def test_with_a_proxy_pool_webrtc_sends_nothing_around_the_proxies(server: LoopbackServer) -> None:
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

    with stun:
        async with BrowserFetcher(proxy_pool=True) as fetcher:
            response = await fetcher.fetch(Request(server.url("/"), render=Render(actions=[gather])))
        with pytest.raises(BlockingIOError):
            stun.recvfrom(2048)  # no STUN request arrived, which would tell the machine's own address

    assert response.browser is not None
    assert response.browser.evaluated == ("complete",)
