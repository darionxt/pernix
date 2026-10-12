from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Response, Spider
from pernix.callbacks import iterate_and_close, run_callbacks, run_errback

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

URL = "https://example.com/list/page-1.html"


class Shop(Spider):
    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield Request("page-2.html")

    async def parse_one(self, response: Response, *, category: str) -> dict[str, str]:
        return {"category": category}

    async def failed(self, request: Request, error: Exception) -> list[Any]:
        return [{"error": str(error)}, Request("retry-later.html")]


async def test_callbacks_get_the_request_kwargs_and_may_return_values() -> None:
    shop = Shop()
    request = Request(URL, callback=shop.parse_one, kwargs={"category": "books"})
    response = Response(url=URL, status=200, headers=[], body=b"", request=request)

    assert [output async for output in run_callbacks(shop)(response)] == [{"category": "books"}]


async def test_errbacks_resolve_relative_urls_against_the_failed_request() -> None:
    shop = Shop()
    request = Request(URL, errback=shop.failed)

    outputs = [output async for output in run_errback(request, ValueError("gone"))]

    error, retry = outputs
    assert error == {"error": "gone"}
    assert isinstance(retry, Request)
    assert retry.url == "https://example.com/list/retry-later.html"


def test_errbacks_need_an_errback() -> None:
    with pytest.raises(ValueError, match="has no errback"):
        run_errback(Request(URL), ValueError("gone"))


async def test_iterate_and_close_closes_generators_left_early() -> None:
    closed: list[bool] = []

    async def numbers() -> AsyncIterator[int]:
        try:
            yield 1
            yield 2
        finally:
            closed.append(True)

    outputs = iterate_and_close(numbers())
    assert await anext(outputs) == 1
    await outputs.aclose()

    assert closed == [True]
