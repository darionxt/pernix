from __future__ import annotations

import asyncio
import subprocess
import sys
import textwrap
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import ComponentError, SettingsError
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.launched_browser import _proxy_settings  # pyright: ignore[reportPrivateUsage]
from pernix.fetchers.refusing_proxy import RefusingProxy

if TYPE_CHECKING:
    from pathlib import Path
from pernix.settings import CORE_SECTIONS, load_settings


@pytest.mark.usefixtures("without_patchright")
async def test_rendering_without_the_extra_fails_with_an_install_hint() -> None:
    async with BrowserFetcher() as fetcher:
        for _ in range(2):  # the second fails at once, without trying again
            with pytest.raises(ComponentError, match=r"pip install 'pernix\[browser\]'"):
                await fetcher.fetch(Request("http://127.0.0.1/", render=True))


@pytest.mark.usefixtures("without_browser_engine")
async def test_rendering_without_the_browser_engine_fails_with_an_install_hint() -> None:
    async with BrowserFetcher() as fetcher:
        with pytest.raises(ComponentError, match=r"install it with: pernix browser install$") as raised:
            await fetcher.fetch(Request("http://127.0.0.1/", render=True))

    assert "executable doesn't exist" in str(raised.value.__cause__).lower()


async def test_a_browser_executable_that_does_not_exist_is_named() -> None:
    settings = BrowserSettings(launch_options={"executable_path": "/nonexistent/chrome"})

    async with BrowserFetcher(settings) as fetcher:
        with pytest.raises(ComponentError) as raised:
            await fetcher.fetch(Request("http://127.0.0.1/", render=True))

    assert str(raised.value) == (
        "the browser executable /nonexistent/chrome does not exist; change browser.launch_options"
    )


def test_the_core_imports_without_patchright() -> None:
    script = textwrap.dedent(
        """
        import sys
        sys.modules["patchright"] = None
        import pernix, pernix.browser, pernix.fetchers, pernix.testing
        from pernix.fetchers import BrowserFetcher
        print(BrowserFetcher.__name__)
        """
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)  # noqa: S603

    assert (result.returncode, result.stdout.strip()) == (0, "BrowserFetcher"), result.stderr


def test_the_browser_settings_and_their_defaults(tmp_path: Path) -> None:
    settings = load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ={}).section(BrowserSettings)

    assert settings == BrowserSettings(headless=True, timeout=30.0, deadline=180.0, launch_options={})


@pytest.mark.parametrize(
    ("values", "problem"),
    [
        ({"browser.timeout": 0}, "browser.timeout"),
        ({"browser.deadline": -1}, "browser.deadline"),
        ({"browser.launch_options": {"headless": False}}, "set 'headless' with browser.headless instead"),
    ],
)
def test_invalid_browser_settings_are_refused(tmp_path: Path, values: dict[str, object], problem: str) -> None:
    with pytest.raises(SettingsError, match=problem):
        load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ={}, overrides=values)


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        ("http://127.0.0.1:3128", {"server": "http://127.0.0.1:3128"}),
        ("localhost:3128", {"server": "http://localhost:3128"}),
        ("socks5://[::1]:1080", {"server": "socks5://[::1]:1080"}),
        (
            "http://us%40er:p%3Ass@proxy.example:8080",
            {"server": "http://proxy.example:8080", "username": "us@er", "password": "p:ss"},
        ),
    ],
)
def test_proxies_are_given_to_the_browser_apart_from_their_credentials(proxy: str, expected: dict[str, str]) -> None:
    assert _proxy_settings(proxy) == expected


async def test_the_refusing_proxy_closes_every_connection() -> None:
    proxy = RefusingProxy()
    with pytest.raises(RuntimeError, match="not started"):
        _ = proxy.url
    await proxy.start()
    try:
        host, port = proxy.url.removeprefix("http://").split(":")
        reader, writer = await asyncio.open_connection(host, int(port))
        writer.write(b"CONNECT example.com:443 HTTP/1.1\r\n\r\n")
        assert await asyncio.wait_for(reader.read(), timeout=5) == b""
        writer.close()
    finally:
        await proxy.close()
