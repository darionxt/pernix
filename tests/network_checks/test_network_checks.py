"""Checks only a real network can make, of what each Fetcher puts on it.

They reach real hosts, so they run only with `--network-checks`, and are meant for a
clean network, without a proxy or a VPN that ends TCP connections or blocks UDP:

    uv run pytest --network-checks tests/network_checks

The check of the egress through a proxy takes the proxy's URL from
`PERNIX_CHECK_PROXY`, such as `http://user:password@proxy.example:8080`, and is
skipped without it.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import orjson
import pytest

from pernix import Request, Session
from pernix.browser import Evaluate, Render
from pernix.fetchers import BrowserFetcher, HTTPFetcher
from pernix.fetchers.pinned_pair import BUNDLED_BROWSER, client_headers
from pernix.settings import DEFAULT_LOCALE

if TYPE_CHECKING:
    from pernix import Response

pytestmark = [pytest.mark.network_check, pytest.mark.usefixtures("browser_engine")]

TRACE = "https://www.cloudflare.com/cdn-cgi/trace"
"""Tells, as `key=value` lines, the HTTP version, the TLS version, and the User-Agent a request came with."""

QUIC_TRACE = "https://cloudflare-quic.com/cdn-cgi/trace"
"""The trace of a host answering with `Alt-Svc: h3`, so that a browser that went there once comes back over HTTP/3."""

ECH_TRACE = "https://crypto.cloudflare.com/cdn-cgi/trace"
"""The trace of a host whose DNS HTTPS records hold an ECH config: its `sni` tells whether a request came with ECH."""

HEADERS = "https://httpbin.org/headers"
"""Answers with the headers of a request, as JSON."""

DELAY = "https://httpbin.org/delay/10"
"""Answers after 10 s."""

ADDRESS = "https://api.ipify.org"
"""Answers with the address a request came from."""

STUN = "stun:stun.l.google.com:19302"


def _trace(response: Response) -> dict[str, str]:
    return dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)


async def _fetch_both(
    url: str, *, session: Session | None = None, proxy: str | None = None
) -> tuple[Response, Response]:
    """Fetch a URL with the HTTP Fetcher, then rendered: the Response of each."""
    async with HTTPFetcher() as http:
        fetched = await http.fetch(Request(url, proxy=proxy), session=session)
    async with BrowserFetcher() as browser:
        rendered = await browser.fetch(Request(url, proxy=proxy, render=True), session=session)
    return fetched, rendered


async def test_both_fetchers_name_the_pinned_pair_to_a_real_server_in_headless_mode() -> None:
    http, browser = await _fetch_both(HEADERS)

    expected = dict(client_headers(DEFAULT_LOCALE))
    for response in (http, browser):
        headers = {name.lower(): value for name, value in orjson.loads(response.body)["headers"].items()}
        assert headers["user-agent"] == expected["User-Agent"]
        assert headers["sec-ch-ua"] == BUNDLED_BROWSER.sec_ch_ua()
        assert headers["sec-ch-ua-platform"] == expected["sec-ch-ua-platform"]


async def test_the_browser_comes_back_over_http3_where_the_http_fetcher_stays_on_http2() -> None:
    session = Session()  # whose context keeps what Alt-Svc told
    async with HTTPFetcher() as http:
        fetched = [_trace(await http.fetch(Request(QUIC_TRACE), session=session))["http"] for _ in range(3)]
    async with BrowserFetcher() as browser:
        rendered = [
            _trace(await browser.fetch(Request(QUIC_TRACE, render=True), session=session))["http"] for _ in range(3)
        ]

    assert fetched == ["http/2"] * 3  # Alt-Svc is not followed (docs/design.md §5.4)
    assert rendered[0] == "http/2"
    assert rendered[-1] == "http/3"


async def test_the_browser_sends_ech_from_dns_where_the_http_fetcher_does_not() -> None:
    http, browser = await _fetch_both(ECH_TRACE)

    assert (_trace(http)["sni"], _trace(browser)["sni"]) == ("plaintext", "encrypted")  # no ECH over HTTP (§5.4)


async def test_requests_through_a_proxy_leave_from_its_address_and_webrtc_tells_no_other() -> None:
    proxy = os.environ.get("PERNIX_CHECK_PROXY")
    if not proxy:
        pytest.skip("PERNIX_CHECK_PROXY names no proxy to check the egress through")
    async with HTTPFetcher() as direct:
        own = (await direct.fetch(Request(ADDRESS))).text.strip()
    http, browser = await _fetch_both(ADDRESS, proxy=proxy, session=Session(proxy=proxy))
    gather = Evaluate(
        f"""() => new Promise(resolve => {{
  const connection = new RTCPeerConnection({{iceServers: [{{urls: "{STUN}"}}]}});
  const candidates = [];
  connection.createDataChannel("probe");
  connection.onicecandidate = event => event.candidate && candidates.push(event.candidate.candidate);
  const done = () => resolve(candidates);
  connection.onicegatheringstatechange = () => connection.iceGatheringState === "complete" && done();
  setTimeout(done, 8000);
  connection.createOffer().then(offer => connection.setLocalDescription(offer));
}})"""
    )
    async with BrowserFetcher(proxy_pool=True) as pooled:
        called = await pooled.fetch(Request(ADDRESS, render=Render(actions=[gather])), session=Session(proxy=proxy))

    egress = http.text.strip()
    assert egress != own
    assert browser.text.strip() == egress
    assert called.browser is not None
    assert not any(own in candidate for candidate in called.browser.evaluated[0])


async def test_a_fetch_cancelled_on_a_real_connection_ends_at_once_and_leaves_the_fetcher_working() -> None:
    async with HTTPFetcher() as http, BrowserFetcher() as browser:
        for fetch in (http.fetch(Request(DELAY)), browser.fetch(Request(DELAY, render=True))):
            task = asyncio.create_task(fetch)
            await asyncio.sleep(1)  # connected, waiting for the answer
            task.cancel()
            async with asyncio.timeout(2):
                with pytest.raises(asyncio.CancelledError):
                    await task
        assert (await http.fetch(Request(TRACE))).status == 200
        assert (await browser.fetch(Request(TRACE, render=True))).status == 200
