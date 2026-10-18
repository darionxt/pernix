from __future__ import annotations

import pytest

from pernix import Request, Spider
from pernix.browser import CapturedResponse, Render, RenderResult
from pernix.testing import make_response

URL = "https://shop.example/books"


class Shop(Spider):
    async def parse_book(self, response: object) -> None:
        pass


def test_a_text_body_makes_an_html_response_to_a_get() -> None:
    response = make_response(URL, "<a href='/book/1'>Dune — 2nd</a>")

    assert (response.url, response.status, response.request.url, response.request.method) == (URL, 200, URL, "GET")
    assert response.headers.get("Content-Type") == "text/html; charset=utf-8"
    assert response.css("a::text").get() == "Dune — 2nd"
    assert response.urljoin("/book/1") == "https://shop.example/book/1"


def test_json_makes_a_json_response() -> None:
    response = make_response(URL, json={"books": ["Dune"]}, status=201)

    assert (response.status, response.json()) == (201, {"books": ["Dune"]})
    assert response.headers.get("Content-Type") == "application/json"
    with pytest.raises(ValueError, match="either body or json"):
        make_response(URL, "x", json={})


def test_bytes_bodies_and_given_headers_are_kept_as_they_are() -> None:
    raw = make_response(URL, b"\x00\x01")
    typed = make_response(URL, "a,b", headers={"Content-Type": "text/csv"})
    decoded = make_response(URL, "caf\xe9".encode("latin-1"), encoding="latin-1")

    assert (raw.body, raw.headers.get("Content-Type")) == (b"\x00\x01", None)
    assert typed.headers.get_all("Content-Type") == ["text/csv"]
    assert decoded.text == "café"


def test_the_request_carries_the_callback_and_meta() -> None:
    shop = Shop()
    request = Request(URL, callback=shop.parse_book, meta={"page": 2})

    response = make_response(URL, "<p>1</p>", request=request)

    assert (response.request.callback, response.meta) == (shop.parse_book, {"page": 2})


def test_the_url_must_be_absolute() -> None:
    with pytest.raises(ValueError, match="absolute URL"):
        make_response("/books", "x", request=Request(URL))


def test_a_response_to_a_plain_request_is_not_rendered() -> None:
    response = make_response(URL, "<p>1</p>")

    assert (response.fetcher, response.browser) == (None, None)


def test_a_response_to_a_rendered_request_is_the_browsers() -> None:
    request = Request(URL, render=Render(wait_for="p"))

    response = make_response(URL, "<p>1</p>", request=request)

    assert (response.fetcher, response.browser, response.request) == ("browser", RenderResult(), request)


def test_a_rendered_response_carries_what_rendering_gave() -> None:
    api = CapturedResponse(f"{URL}/api", 200, body=b'{"books": 2}')
    browser = RenderResult(captured=[api], screenshots=[b"png"], evaluated=[42])

    response = make_response(URL, "<p>1</p>", browser=browser)

    assert response.browser is browser
    assert (response.fetcher, response.request.render) == ("browser", True)
    assert (browser.captured, browser.screenshots, browser.evaluated, browser.document) == (
        (api,),
        (b"png",),
        (42,),
        None,
    )
