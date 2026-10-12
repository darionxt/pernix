from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from pernix import Request, Response
from pernix.errors import ComponentError
from pernix.middlewares import Middleware, NodeMiddleware, build_download_chain, middleware

if TYPE_CHECKING:
    from pernix.middlewares import Download

URL = "https://example.com/"


class FakeFetch:
    """The innermost step: answers with queued statuses, or raises queued exceptions."""

    def __init__(self, *outcomes: int | Exception) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[Request] = []

    async def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        outcome = self.outcomes.pop(0) if self.outcomes else 200
        if isinstance(outcome, Exception):
            raise outcome
        return _response(request, outcome)


def _response(request: Request, status: int = 200, body: bytes = b"") -> Response:
    return Response(url=request.url, status=status, headers=[], body=body, request=request)


async def _fetch(download: Download) -> Response:
    return await download(Request(URL))


class Recorder(Middleware):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    async def __call__(self, request: Request, call_next: Download) -> Response:
        self.log.append(f"{self.name} in")
        try:
            return await call_next(request)
        finally:
            self.log.append(f"{self.name} out")


async def test_layers_run_symmetrically_outermost_first() -> None:
    log: list[str] = []
    download = build_download_chain([Recorder("outer", log), Recorder("inner", log)], FakeFetch())

    await download(Request(URL))

    assert log == ["outer in", "inner in", "inner out", "outer out"]


async def test_a_layer_can_change_the_request_and_the_response() -> None:
    class Sign(Middleware):
        async def __call__(self, request: Request, call_next: Download) -> Response:
            response = await call_next(request.replace(headers={"X-Sign": "abc"}))
            return response.replace(status=299)

    fetch = FakeFetch()
    response = await build_download_chain([Sign()], fetch)(Request(URL))

    assert fetch.requests[0].headers.get("X-Sign") == "abc"
    assert response.status == 299


async def test_a_cache_short_circuits_the_inner_layers() -> None:
    class Cache(Middleware):
        def __init__(self) -> None:
            self.stored: dict[str, Response] = {}

        async def __call__(self, request: Request, call_next: Download) -> Response:
            if request.url not in self.stored:
                self.stored[request.url] = await call_next(request)
            return self.stored[request.url]

    log: list[str] = []
    fetch = FakeFetch()
    download = build_download_chain([Cache(), Recorder("inner", log)], fetch)

    first = await download(Request(URL))
    second = await download(Request(URL))

    assert second is first
    assert len(fetch.requests) == 1
    assert log == ["inner in", "inner out"]


async def test_call_next_may_run_more_than_once() -> None:
    class RetryUnavailable(Middleware):
        async def __call__(self, request: Request, call_next: Download) -> Response:
            response = await call_next(request)
            for _ in range(2):
                if response.status != 503:
                    break
                response = await call_next(request)
            return response

    fetch = FakeFetch(503, 503, 200)
    response = await build_download_chain([RetryUnavailable()], fetch)(Request(URL))

    assert response.status == 200
    assert len(fetch.requests) == 3


async def test_exceptions_travel_out_through_every_layer() -> None:
    log: list[str] = []

    class Fallback(Middleware):
        async def __call__(self, request: Request, call_next: Download) -> Response:
            try:
                return await call_next(request)
            except ConnectionError as exc:
                log.append(f"recovered from {exc}")
                return _response(request, 599)

    download = build_download_chain(
        [Fallback(), Recorder("middle", log), Recorder("inner", log)], FakeFetch(ConnectionError("refused"))
    )

    response = await download(Request(URL))

    assert response.status == 599
    assert log == ["middle in", "inner in", "inner out", "middle out", "recovered from refused"]


async def test_function_middleware() -> None:
    seen: list[str] = []

    @middleware
    async def tag(request: Request, call_next: Download) -> Response:
        seen.append(request.url)
        return await call_next(request)

    await build_download_chain([tag], FakeFetch())(Request(URL))

    assert isinstance(tag, Middleware)
    assert seen == [URL]
    assert repr(tag) == "<middleware test_middleware.test_function_middleware.<locals>.tag>"


def test_function_middleware_must_be_async() -> None:
    def blocking(request: Request, call_next: Download) -> Response:
        raise NotImplementedError

    with pytest.raises(TypeError, match="@middleware needs an 'async def' function"):
        middleware(blocking)  # type: ignore[arg-type]


def test_layers_must_be_async_callables() -> None:
    class Blocking:
        def __call__(self, request: Request, call_next: Download) -> Response:
            raise NotImplementedError

    with pytest.raises(ComponentError, match="is not an async callable"):
        build_download_chain([Blocking()], FakeFetch())
    with pytest.raises(ComponentError, match="is not an async callable"):
        build_download_chain([object()], FakeFetch())


async def test_cancellation_is_never_turned_into_a_response() -> None:
    class Swallow(Middleware):
        async def __call__(self, request: Request, call_next: Download) -> Response:
            try:
                return await call_next(request)
            except Exception:
                return _response(request, 599)

    started = asyncio.Event()

    async def hang(request: Request) -> Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(_fetch(build_download_chain([Swallow()], hang)))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


# NodeMiddleware


class Hooks(NodeMiddleware):
    def __init__(self, log: list[str]) -> None:
        self.log = log

    async def process_request(self, request: Request) -> Request | Response | None:
        self.log.append(f"request {request.url}")
        return None

    async def process_response(self, request: Request, response: Response) -> Request | Response:
        self.log.append(f"response {response.status}")
        return response

    async def process_exception(self, request: Request, exception: Exception) -> Request | Response | None:
        self.log.append(f"exception {exception!r}")
        return None


class SyncHooks(NodeMiddleware):
    def __init__(self, log: list[str]) -> None:
        self.log = log

    def process_request(self, request: Request) -> Request | Response | None:
        self.log.append(f"request {request.url}")
        return None

    def process_response(self, request: Request, response: Response) -> Request | Response:
        self.log.append(f"response {response.status}")
        return response

    def process_exception(self, request: Request, exception: Exception) -> Request | Response | None:
        self.log.append(f"exception {exception!r}")
        return _response(request, 504)


async def test_node_hooks_run_around_the_inner_layers() -> None:
    log: list[str] = []

    response = await build_download_chain([Hooks(log)], FakeFetch(201))(Request(URL))

    assert response.status == 201
    assert log == [f"request {URL}", "response 201"]


async def test_node_hooks_may_be_plain_methods() -> None:
    log: list[str] = []
    error = ConnectionError("reset")

    response = await build_download_chain([SyncHooks(log)], FakeFetch(error))(Request(URL))

    assert response.status == 504
    assert log == [f"request {URL}", f"exception {error!r}", "response 504"]


async def test_a_node_response_from_process_request_skips_the_inner_layers() -> None:
    class Cached(Hooks):
        async def process_request(self, request: Request) -> Request | Response | None:
            await super().process_request(request)
            return _response(request, 203)

    log: list[str] = []
    fetch = FakeFetch()

    response = await build_download_chain([Cached(log)], fetch)(Request(URL))

    assert response.status == 203
    assert fetch.requests == []
    assert log == [f"request {URL}", "response 203"]


async def test_node_exceptions_propagate_unless_recovered() -> None:
    log: list[str] = []
    error = TimeoutError("slow")

    with pytest.raises(TimeoutError):
        await build_download_chain([Hooks(log)], FakeFetch(error))(Request(URL))
    assert log == [f"request {URL}", f"exception {error!r}"]

    class Recovering(Hooks):
        async def process_exception(self, request: Request, exception: Exception) -> Request | Response | None:
            await super().process_exception(request, exception)
            return _response(request, 504)

    log.clear()
    response = await build_download_chain([Recovering(log)], FakeFetch(error))(Request(URL))

    assert response.status == 504
    assert log == [f"request {URL}", f"exception {error!r}", "response 504"]


async def test_a_node_request_is_fetched_instead_starting_at_that_layer() -> None:
    class Redirecting(Hooks):
        async def process_response(self, request: Request, response: Response) -> Request | Response:
            self.log.append(f"response {response.status}")
            if response.status == 302:
                return Request("https://example.com/moved")
            return response

    log: list[str] = []
    fetch = FakeFetch(302, 200)

    response = await build_download_chain([Redirecting(log)], fetch)(Request(URL))

    assert response.url == "https://example.com/moved"
    assert [request.url for request in fetch.requests] == [URL, "https://example.com/moved"]
    assert log == [f"request {URL}", "response 302", "request https://example.com/moved", "response 200"]


async def test_node_hooks_must_return_responses_or_requests() -> None:
    class Broken(NodeMiddleware):
        def process_response(self, request: Request, response: Response) -> Request | Response:
            return "oops"  # type: ignore[return-value]

    with pytest.raises(TypeError, match=r"process_response of .*Broken.* returned 'oops', which is not a Response"):
        await build_download_chain([Broken()], FakeFetch())(Request(URL))


async def test_node_hooks_never_see_cancellation() -> None:
    log: list[str] = []
    started = asyncio.Event()

    async def hang(request: Request) -> Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    task = asyncio.create_task(_fetch(build_download_chain([Hooks(log)], hang)))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert log == [f"request {URL}"]
