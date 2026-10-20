"""The Profile contract through a proxy: the `CONNECT` each Fetcher sends, and a request through a proxy without a tunnel."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

import pytest

from pernix import Request
from pernix.errors import FetchError
from pernix.fetchers import BrowserFetcher, HTTPFetcher
from pernix.fetchers.pinned_pair import client_headers
from pernix.settings import DEFAULT_LOCALE

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

type Head = tuple[str, list[tuple[str, str]]]
"""A request line, and the headers after it, in order."""

PAGE = b"<!doctype html><link rel='icon' href='data:,'><p>proxied</p>"
CREDENTIALS = "user:secret@"
AUTHORIZATION = ("Proxy-Authorization", "Basic dXNlcjpzZWNyZXQ=")


class RecordingProxy:
    """An HTTP proxy on 127.0.0.1 that records each request head.

    It answers a `CONNECT` with 502, so that no tunnel opens, and a request
    without a tunnel with a page. With `credentials`, it asks for them first.
    """

    def __init__(self, *, credentials: bool = False) -> None:
        self.credentials = credentials
        self.heads: list[Head] = []
        self._server: asyncio.Server | None = None

    def url(self) -> str:
        assert self._server is not None
        port = self._server.sockets[0].getsockname()[1]
        return f"http://{CREDENTIALS if self.credentials else ''}127.0.0.1:{port}"

    def sent(self, line: str) -> list[Head]:
        """The heads of the requests with a request line, in order."""
        return [head for head in self.heads if head[0] == line]

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._serve, "127.0.0.1", 0)

    async def close(self) -> None:
        assert self._server is not None
        self._server.close()
        self._server.close_clients()
        await self._server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                head = _head(await reader.readuntil(b"\r\n\r\n"))
                self.heads.append(head)
                names = {name.lower() for name, _ in head[1]}
                if self.credentials and "proxy-authorization" not in names:
                    writer.write(
                        b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                        b'Proxy-Authenticate: Basic realm="contract"\r\nContent-Length: 0\r\n\r\n'
                    )
                elif head[0].startswith("CONNECT "):
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    break
                else:
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n%s"
                        % (len(PAGE), PAGE)
                    )
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


def _head(raw: bytes) -> Head:
    line, *fields = raw.decode("latin-1").rstrip("\r\n").split("\r\n")
    return line, [(name, value.strip()) for name, _, value in (field.partition(":") for field in fields)]


@contextlib.asynccontextmanager
async def _proxy(*, credentials: bool = False) -> AsyncIterator[RecordingProxy]:
    proxy = RecordingProxy(credentials=credentials)
    await proxy.start()
    try:
        yield proxy
    finally:
        await proxy.close()


async def _fetch_with_both(url: str, *, credentials: bool = False) -> tuple[RecordingProxy, RecordingProxy]:
    """Fetch a URL through a proxy with each Fetcher, rendered for the browser: the proxies, which recorded each."""
    proxies: list[RecordingProxy] = []
    for fetcher in (BrowserFetcher(), HTTPFetcher()):
        async with _proxy(credentials=credentials) as proxy, fetcher:
            with contextlib.suppress(FetchError):  # no tunnel opens
                await fetcher.fetch(Request(url, proxy=proxy.url(), render=isinstance(fetcher, BrowserFetcher)))
        proxies.append(proxy)
    return proxies[0], proxies[1]


def _windowed(head: Head) -> Head:
    """A head with the User-Agent the browser sends with a window, where it names itself `HeadlessChrome`."""
    line, fields = head
    return line, [(name, value.replace("HeadlessChrome/", "Chrome/")) for name, value in fields]


USER_AGENT = dict(client_headers(DEFAULT_LOCALE))["User-Agent"]

CONNECT = "CONNECT site.test:443 HTTP/1.1"
PLAIN = "GET http://site.test/page HTTP/1.1"


async def test_the_http_fetcher_sends_the_connect_chrome_sends() -> None:
    async with _proxy() as proxy, HTTPFetcher() as fetcher:
        with pytest.raises(FetchError):
            await fetcher.fetch(Request("https://site.test/page", proxy=proxy.url()))

    assert proxy.sent(CONNECT)[0] == (
        CONNECT,
        [("Host", "site.test:443"), ("Proxy-Connection", "keep-alive"), ("User-Agent", USER_AGENT)],
    )


async def test_the_http_fetcher_sends_a_request_through_a_proxy_as_chrome_does() -> None:
    async with _proxy(credentials=True) as proxy, HTTPFetcher() as fetcher:
        await fetcher.fetch(Request("http://site.test/page", proxy=proxy.url()))

    [(_line, fields)] = proxy.sent(PLAIN)
    assert [name for name, _ in fields][:4] == [
        "Host",
        "Proxy-Connection",
        "Proxy-Authorization",
        "Upgrade-Insecure-Requests",
    ]
    assert ("Proxy-Connection", "keep-alive") in fields
    assert AUTHORIZATION in fields
    assert "Connection" not in [name for name, _ in fields]


@pytest.mark.usefixtures("browser_engine")
async def test_both_fetchers_send_one_connect_but_for_the_headless_browsers_user_agent() -> None:
    browser, http = await _fetch_with_both("https://site.test/page")

    assert _windowed(browser.sent(CONNECT)[0]) == http.sent(CONNECT)[0]


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.xfail(
    reason="in headless mode, Chrome names itself HeadlessChrome in its CONNECT, which the User-Agent of its "
    "contexts does not reach; with a window it names itself Chrome, as the HTTP Fetcher does",
    strict=True,
)
async def test_the_headless_browser_sends_the_user_agent_of_its_pages_in_its_connect() -> None:
    browser, http = await _fetch_with_both("https://site.test/page")

    assert browser.sent(CONNECT)[0] == http.sent(CONNECT)[0]


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.parametrize("credentials", [False, True], ids=["without credentials", "with credentials"])
async def test_both_fetchers_send_one_request_through_a_proxy_without_a_tunnel(credentials: bool) -> None:
    browser, http = await _fetch_with_both("http://site.test/page", credentials=credentials)

    # Chrome waits for the proxy to ask for credentials; curl sends them at once, where Chrome then does.
    assert browser.sent(PLAIN)[-1] == http.sent(PLAIN)[0]


@pytest.mark.usefixtures("browser_engine")
@pytest.mark.xfail(
    reason="Chrome sends its first CONNECT through a proxy without credentials, and once asked for them adds "
    "Proxy-Authorization last; curl sends them at once, after Host",
    strict=True,
)
async def test_both_fetchers_send_one_connect_with_credentials() -> None:
    browser, http = await _fetch_with_both("https://site.test/page", credentials=True)

    assert [_windowed(head) for head in browser.sent(CONNECT)] == http.sent(CONNECT)
