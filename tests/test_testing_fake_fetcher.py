from __future__ import annotations

import re
from typing import Any

import pytest

from pernix import Request
from pernix.errors import Timeout
from pernix.testing import FakeFetcher, make_response

SHOP = "https://shop.example"


async def _body(fake: FakeFetcher, url: str, **options: Any) -> bytes:
    return (await fake.fetch(Request(url, **options))).body


async def test_routes_match_urls_exactly_or_patterns_fully_and_the_first_added_answers() -> None:
    fake = FakeFetcher(
        {
            f"{SHOP}/book/1": "first edition",
            re.compile(r"https://shop\.example/book/\d+"): "any book",
            re.compile(r"https://shop\.example/book"): "never: matches only a part of the longer URLs",
        }
    )

    assert await _body(fake, f"{SHOP}/book/1") == b"first edition"
    assert await _body(fake, f"{SHOP}/book/22") == b"any book"
    assert (await fake.fetch(Request(f"{SHOP}/book/1?"))).status == 404  # exact means exact
    assert [request.url for request in fake.unmatched] == [f"{SHOP}/book/1?"]


async def test_a_route_may_answer_only_one_method() -> None:
    fake = FakeFetcher()
    fake.add(f"{SHOP}/login", "welcome", method="post")
    fake.add(f"{SHOP}/login", "<form></form>")

    assert await _body(fake, f"{SHOP}/login", method="POST") == b"welcome"
    assert await _body(fake, f"{SHOP}/login") == b"<form></form>"


async def test_a_list_of_answers_is_used_in_turn_and_its_last_repeats() -> None:
    fake = FakeFetcher({f"{SHOP}/flaky": [Timeout("slow"), Timeout("slower"), "ok"]})

    for message in ("slow", "slower"):
        with pytest.raises(Timeout, match=message):
            await fake.fetch(Request(f"{SHOP}/flaky"))
    assert [await _body(fake, f"{SHOP}/flaky") for _ in range(2)] == [b"ok", b"ok"]


async def test_an_exception_raised_again_does_not_pile_up_tracebacks() -> None:
    fake = FakeFetcher({f"{SHOP}/down": Timeout("down")})
    depths: list[int] = []

    for _ in range(3):
        with pytest.raises(Timeout) as raised:
            await fake.fetch(Request(f"{SHOP}/down"))
        depth, traceback = 0, raised.value.__traceback__
        while traceback is not None:
            depth, traceback = depth + 1, traceback.tb_next
        depths.append(depth)

    assert depths[0] == depths[1] == depths[2]


async def test_functions_answer_from_the_request_sync_or_async() -> None:
    async def search(request: Request) -> str:
        return f"results for {request.url.rpartition('=')[2]}"

    fake = FakeFetcher(
        {
            re.compile(r".*/echo"): lambda request: request.headers.get("X-Echo") or "",
            re.compile(r".*/search\?q=.*"): search,
        }
    )

    assert await _body(fake, f"{SHOP}/echo", headers={"X-Echo": "ping"}) == b"ping"
    assert await _body(fake, f"{SHOP}/search?q=dune") == b"results for dune"


async def test_a_response_answer_answers_the_request_fetched() -> None:
    redirected = make_response(f"{SHOP}/books/", "<p>moved</p>", status=200)
    fake = FakeFetcher({f"{SHOP}/books": redirected})
    request = Request(f"{SHOP}/books", meta={"page": 1})

    response = await fake.fetch(request)

    assert (response.url, response.request, response.meta) == (f"{SHOP}/books/", request, {"page": 1})


async def test_unmatched_requests_get_404_and_are_recorded_like_every_request() -> None:
    fake = FakeFetcher({f"{SHOP}/": "home"})

    home = await fake.fetch(Request(f"{SHOP}/"))
    missing = await fake.fetch(Request(f"{SHOP}/missing"))

    assert (home.status, missing.status) == (200, 404)
    assert [request.url for request in fake.requests] == [f"{SHOP}/", f"{SHOP}/missing"]
    assert [request.url for request in fake.unmatched] == [f"{SHOP}/missing"]


async def test_answers_of_other_kinds_and_empty_lists_are_refused() -> None:
    fake = FakeFetcher({f"{SHOP}/": 42})  # type: ignore[dict-item]

    with pytest.raises(TypeError, match=r"answered <Request GET https://shop\.example/> with 42"):
        await fake.fetch(Request(f"{SHOP}/"))
    with pytest.raises(ValueError, match="needs at least one answer"):
        fake.add(f"{SHOP}/other", [])


def test_it_fetches_http_and_https() -> None:
    assert FakeFetcher.schemes == {"http", "https"}
