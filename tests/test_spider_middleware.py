from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request, Response, Spider
from pernix.engine import run_callbacks
from pernix.errors import ComponentError
from pernix.spider_middleware import SpiderMiddleware, build_spider_chain, spider_middleware

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from pernix.spider_middleware import HandleResponse

URL = "https://example.com/list/index.html"


class Shop(Spider):
    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"title": "a"}
        yield None
        yield {}
        yield Request("page-2.html")

    async def parse_broken(self, response: Response) -> AsyncIterator[Any]:
        yield {"title": "before"}
        raise ValueError("cannot parse")


def _response(callback_name: str | None = None, url: str = URL) -> Response:
    shop = Shop()
    callback = None if callback_name is None else getattr(shop, callback_name)
    return Response(url=url, status=200, headers=[], body=b"", request=Request(url, callback=callback))


async def _collect(handle: HandleResponse, response: Response) -> list[Any]:
    return [output async for output in handle(response)]


class DropEmpty(SpiderMiddleware):
    async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
        async for output in call_next(response):
            if output:
                yield output


class Stamp(SpiderMiddleware):
    async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
        async for output in call_next(response):
            yield {**output, "status": response.status} if isinstance(output, dict) else output
        yield {"summary": True}


async def test_middleware_filter_add_and_transform_outputs() -> None:
    handle = build_spider_chain([Stamp(), DropEmpty()], run_callbacks(Shop()))

    outputs = await _collect(handle, _response())

    assert outputs[0] == {"title": "a", "status": 200}
    assert isinstance(outputs[1], Request)
    assert outputs[1].url == "https://example.com/list/page-2.html"
    assert outputs[2:] == [{"summary": True}]


async def test_without_middleware_the_callback_outputs_pass_through() -> None:
    outputs = await _collect(run_callbacks(Shop()), _response())

    assert outputs[:3] == [{"title": "a"}, None, {}]


async def test_middleware_sees_callback_exceptions() -> None:
    caught: list[Exception] = []

    class Report(SpiderMiddleware):
        async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
            try:
                async for output in call_next(response):
                    yield output
            except ValueError as exc:
                caught.append(exc)
                yield {"error": str(exc)}

    outputs = await _collect(build_spider_chain([Report()], run_callbacks(Shop())), _response("parse_broken"))

    assert outputs == [{"title": "before"}, {"error": "cannot parse"}]
    assert [str(exc) for exc in caught] == ["cannot parse"]


async def test_callback_exceptions_propagate_through_middleware_that_ignores_them() -> None:
    handle = build_spider_chain([Stamp(), DropEmpty()], run_callbacks(Shop()))
    outputs: list[Any] = []

    async def drain() -> None:
        async for output in handle(_response("parse_broken")):
            outputs.append(output)  # noqa: PERF401  # keeps the outputs yielded before the exception

    with pytest.raises(ValueError, match="cannot parse"):
        await drain()
    assert outputs == [{"title": "before", "status": 200}]


async def test_middleware_may_hand_the_callback_another_response() -> None:
    class Mirror(SpiderMiddleware):
        async def __call__(self, response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
            async for output in call_next(response.replace(url="https://mirror.example/list/")):
                yield output

    outputs = await _collect(build_spider_chain([Mirror()], run_callbacks(Shop())), _response())

    assert outputs[-1].url == "https://mirror.example/list/page-2.html"


async def test_function_spider_middleware() -> None:
    @spider_middleware
    async def only_requests(response: Response, call_next: HandleResponse) -> AsyncIterator[Any]:
        async for output in call_next(response):
            if isinstance(output, Request):
                yield output

    outputs = await _collect(build_spider_chain([only_requests], run_callbacks(Shop())), _response())

    assert isinstance(only_requests, SpiderMiddleware)
    assert [output.url for output in outputs] == ["https://example.com/list/page-2.html"]


def test_function_spider_middleware_must_be_an_async_generator() -> None:
    async def not_a_generator(response: Response, call_next: HandleResponse) -> None:
        pass

    with pytest.raises(TypeError, match="@spider_middleware needs an async generator function"):
        spider_middleware(not_a_generator)  # type: ignore[arg-type]


def test_layers_must_be_async_generators() -> None:
    class Coroutine:
        async def __call__(self, response: Response, call_next: HandleResponse) -> list[Any]:
            return []

    with pytest.raises(ComponentError, match="is not an async generator"):
        build_spider_chain([Coroutine()], run_callbacks(Shop()))
