"""How the browser Fetcher checks the redirects of a page's navigation before the browser follows them."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import pytest

from pernix import IgnoreRequest, Request
from pernix.errors import ProxyError
from pernix.fetchers import BrowserFetcher
from pernix.fetchers.redirects import checking_redirects
from pernix.sending import NotYet
from pernix.testing import LoopbackServer

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]


@pytest.fixture
def other_server() -> Iterator[LoopbackServer]:
    with LoopbackServer() as server:
        yield server


class Checks:
    """Records the redirects it checks, refusing those that lead to a path with an error."""

    def __init__(self, refused: str | None = None, error: Exception | None = None) -> None:
        self.refused = refused
        self.error = error or IgnoreRequest("refused")
        self.urls: list[str] = []

    async def __call__(self, url: str) -> None:
        self.urls.append(url)
        if urlsplit(url).path == self.refused:
            raise self.error


def _render(url: str, **kwargs: object) -> Request:
    return Request(url, render=True, **kwargs)  # pyright: ignore[reportArgumentType]


def _paths(server: LoopbackServer) -> list[str]:
    return [hop.path for hop in server.received]


async def test_inside_checking_redirects_each_redirect_of_the_page_is_checked_before_it_is_followed(
    server: LoopbackServer, other_server: LoopbackServer
) -> None:
    server.route("/a", status=302, headers=[("Location", "/b")])
    server.route("/b", status=307, headers=[("Location", other_server.url("/c"))])
    other_server.route("/c", "<p>c</p><iframe src='/frame'></iframe>", headers=HTML)
    other_server.route("/frame", status=302, headers=[("Location", "/framed")])  # not the page's own
    other_server.route("/framed", "<p>framed</p>", headers=HTML)
    checks = Checks()

    async with BrowserFetcher() as fetcher:
        with checking_redirects(checks):
            response = await fetcher.fetch(_render(server.url("/a")))
        await fetcher.fetch(_render(server.url("/a")))  # outside the block, none is checked

    assert response.url == other_server.url("/c")
    assert [hop.url for hop in response.history] == [server.url("/a"), server.url("/b")]
    assert checks.urls == [server.url("/b"), other_server.url("/c")]
    assert _paths(other_server).count("/framed") == 2


@pytest.mark.parametrize(
    "error", [IgnoreRequest("robots.txt"), NotYet(60.0, "robots.txt unreachable"), ProxyError("proxy down")]
)
async def test_a_redirect_the_check_refuses_fails_the_fetch_with_what_the_check_raised(
    server: LoopbackServer, error: Exception
) -> None:
    server.route("/a", status=302, headers=[("Location", "/b")])
    server.route("/b", status=302, headers=[("Location", "/c")])
    server.route("/c", "<p>c</p>", headers=HTML)

    async with BrowserFetcher() as fetcher:
        with checking_redirects(Checks(refused="/c", error=error)), pytest.raises(type(error)) as raised:
            await fetcher.fetch(_render(server.url("/a")))
        response = await fetcher.fetch(_render(server.url("/a")))  # the browser is still usable

    assert raised.value is error
    assert _paths(server) == ["/a", "/b", "/a", "/b", "/c"]
    assert response.url == server.url("/c")


async def test_the_method_body_and_headers_of_a_request_stay_on_the_redirects_checked(server: LoopbackServer) -> None:
    server.route("/search", status=303, headers=[("Location", "/results")], method="POST")
    server.route("/results", "<p>found</p>", headers=HTML)
    checks = Checks()

    async with BrowserFetcher() as fetcher:
        with checking_redirects(checks):
            response = await fetcher.fetch(
                _render(server.url("/search"), data={"q": "dune"}, headers={"X-Token": "secret"})
            )

    assert response.url == server.url("/results")
    search, results = server.received
    assert (search.method, search.body) == ("POST", b"q=dune")
    assert (results.method, results.header("X-Token")) == ("GET", "secret")
    assert checks.urls == [server.url("/results")]


async def test_a_fetch_cancelled_while_a_redirect_is_checked_cancels_the_check(server: LoopbackServer) -> None:
    server.route("/a", status=302, headers=[("Location", "/b")])
    server.route("/b", "<p>b</p>", headers=HTML)
    reached = asyncio.Event()
    cancelled: list[str] = []

    async def stalls(url: str) -> None:
        reached.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(url)
            raise

    async with BrowserFetcher() as fetcher:
        with checking_redirects(stalls):
            fetching = asyncio.create_task(fetcher.fetch(_render(server.url("/a"))))
        async with asyncio.timeout(30):
            await reached.wait()
        fetching.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fetching
        response = await fetcher.fetch(_render(server.url("/b")))

    assert cancelled == [server.url("/b")]
    assert response.url == server.url("/b")
