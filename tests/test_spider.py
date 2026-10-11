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


async def test_default_parse_must_be_implemented() -> None:
    response = Response(
        url="https://example.com/", status=200, headers=Headers(), body=b"", request=Request("https://example.com/")
    )
    outputs: AsyncIterator[Any] = Spider().parse(response)

    with pytest.raises(NotImplementedError, match=r"Spider\.parse\(\) is not implemented"):
        await anext(outputs)
