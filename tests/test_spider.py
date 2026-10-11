from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Headers, Request, Response, Spider

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


def test_spider_name_defaults_to_class_name() -> None:
    class ProductListing(Spider):
        pass

    assert ProductListing.name == "productlisting"


async def test_start_yields_a_get_per_start_url() -> None:
    class Listing(Spider):
        start_urls = ("https://example.com/a", "https://example.com/b")

    requests = [request async for request in Listing().start()]

    assert [(r.method, r.url, r.callback) for r in requests] == [
        ("GET", "https://example.com/a", None),
        ("GET", "https://example.com/b", None),
    ]


def test_default_parse_must_be_implemented() -> None:
    response = Response(
        url="https://example.com/", status=200, headers=Headers(), body=b"", request=Request("https://example.com/")
    )

    with pytest.raises(NotImplementedError, match=r"Spider\.parse\(\) is not implemented"):
        Spider().parse(response)


def test_generator_and_coroutine_overrides_satisfy_the_parse_contract() -> None:
    # Type-checked: both override styles must be accepted without suppressions.
    class Generating(Spider):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"url": response.url}

    class Returning(Spider):
        async def parse(self, response: Response) -> list[dict[str, str]]:
            return [{"url": response.url}]

    assert Generating.parse is not Spider.parse
    assert Returning.parse is not Spider.parse
