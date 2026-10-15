from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Request, Response, Spider, events
from pernix.crawler import Crawler
from pernix.extensions import OffsiteFilter

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from pernix.testing import LoopbackServer


def _filter(*domains: str) -> OffsiteFilter:
    class Scoped(Spider):
        allowed_domains: ClassVar[Sequence[str]] = domains

    return OffsiteFilter(Crawler.from_spider(Scoped, settings={}))


@pytest.mark.parametrize(
    ("url", "admitted"),
    [
        ("https://example.com/", True),
        ("https://shop.example.com:8443/a", True),
        ("https://deep.shop.EXAMPLE.com./a", True),
        ("https://books.test/", True),
        ("https://example.org/", False),
        ("https://notexample.com/", False),
        ("https://example.com.evil.net/", False),
    ],
)
def test_allowed_domains_include_their_subdomains(url: str, admitted: bool) -> None:
    offsite = _filter("Example.com", "books.test.")

    veto = offsite.admit(events.RequestScheduled(Request(url)))

    assert (veto is None) is admitted
    if veto is not None:
        assert veto.reason == "offsite"


@pytest.mark.parametrize(
    ("allowed", "url"),
    [
        ("例子.测试", "https://xn--fsqu00a.xn--0zwm56d/"),
        ("例子.测试", "https://shop.例子.测试/"),
        ("xn--fsqu00a.xn--0zwm56d", "https://例子.测试/a"),
        ("XN--FSQU00A.xn--0zwm56d", "https://www.xn--fsqu00a.xn--0zwm56d/"),
        ("127.1", "http://127.1/"),
        ("127.1", "http://127.0.0.1:8080/"),
        ("example.com\u3002", "https://example.com/"),
        ("example.com", "https://example.com\u3002/"),
    ],
)
def test_allowed_domains_and_hosts_are_normalized_alike(allowed: str, url: str) -> None:
    assert _filter(allowed).admit(events.RequestScheduled(Request(url))) is None


def test_spiders_without_allowed_domains_are_not_filtered() -> None:
    assert _filter().admit(events.RequestScheduled(Request("https://anywhere.example/"))) is None


@pytest.mark.parametrize("domain", ["https://example.com", "example.com:443", "example.com/shop", ""])
def test_allowed_domains_must_be_bare_domains(domain: str) -> None:
    with pytest.raises(ValueError, match=r"write bare domains such as 'example\.com'"):
        _filter(domain)


async def test_offsite_requests_are_dropped_at_admission_and_reported(server: LoopbackServer) -> None:
    links = "".join(f"<a href='{href}'>x</a>" for href in ("/page", "https://elsewhere.example/a"))
    server.route("/", links)
    server.route("/page", "<p>page</p>")
    dropped: list[tuple[str, str]] = []

    class Watch:
        @events.on(events.RequestDropped)
        def drop(self, event: events.RequestDropped) -> None:
            dropped.append((event.request.url, event.reason))

    class Local(Spider):
        start_urls = (server.url("/"),)
        allowed_domains = ("127.0.0.1",)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            for href in response.css("a::attr(href)").getall():
                yield Request(href, dont_filter=True)  # dont_filter does not bypass the offsite check

    async with asyncio.timeout(10):
        await Local.crawl(settings={"extensions": {Watch: 1}})

    assert dropped == [("https://elsewhere.example/a", "offsite")]
    assert [request.path for request in server.received] == ["/", "/page"]


async def test_the_offsite_filter_can_be_turned_off(server: LoopbackServer) -> None:
    server.route("/", "ok")

    class Elsewhere(Spider):
        start_urls = (server.url("/"),)
        allowed_domains = ("example.com",)

        async def parse(self, response: Response) -> None:
            pass

    async with asyncio.timeout(10):
        await Elsewhere.crawl(settings={"extensions": {"pernix.extensions.OffsiteFilter": None}})

    assert [request.path for request in server.received] == ["/"]
