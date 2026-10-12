from __future__ import annotations

from typing import Any

import orjson
import pytest

from pernix import Request, Response, Spider
from pernix.request import resolve_relative_urls

URL = "https://example.com/"


class Shop(Spider):
    async def parse_item(self, response: Response) -> None:
        pass


def test_request_is_immutable_and_replaceable() -> None:
    request = Request(URL, headers={"A": "1"}, meta={"k": "v"})

    with pytest.raises(AttributeError):
        request.url = "https://example.com/other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        request.meta["k"] = "w"  # type: ignore[index]

    moved = request.replace(url="https://example.com/other", method="post")
    assert (moved.url, moved.method, moved.headers.get("a")) == ("https://example.com/other", "POST", "1")
    assert request.url == URL


def test_defaults() -> None:
    request = Request(URL)

    assert (request.method, request.body, request.callback, request.errback) == ("GET", None, None, None)
    assert (dict(request.kwargs), dict(request.meta), dict(request.cookies), dict(request.ext)) == ({}, {}, {}, {})
    assert (request.priority, request.dont_filter, request.fingerprint, request.render) == (0, False, None, False)
    assert (request.session, request.fetcher, request.timeout, request.proxy, request.impersonate) == (
        None,
        None,
        None,
        None,
        None,
    )


def test_str_bodies_are_encoded_as_utf8() -> None:
    assert Request(URL, method="POST", body="名字").body == "名字".encode()


def test_json_sets_the_body_and_a_default_content_type() -> None:
    request = Request(URL, method="POST", json={"q": "books", "page": 2})
    custom = Request(URL, method="POST", json=[1], headers={"content-type": "application/vnd.api+json"})

    assert orjson.loads(request.body or b"") == {"q": "books", "page": 2}
    assert request.headers.get("Content-Type") == "application/json"
    assert custom.headers.get_all("Content-Type") == ["application/vnd.api+json"]


@pytest.mark.parametrize(
    "data",
    [{"q": "a b", "tag": ["x", "y"]}, [("q", "a b"), ("tag", "x"), ("tag", "y")]],
)
def test_data_is_form_encoded(data: Any) -> None:
    request = Request(URL, method="POST", data=data)

    assert request.body == b"q=a+b&tag=x&tag=y"
    assert request.headers.get("Content-Type") == "application/x-www-form-urlencoded"


def test_body_json_and_data_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        Request(URL, body=b"x", json={"a": 1})
    with pytest.raises(ValueError, match="mutually exclusive"):
        Request(URL, json={"a": 1}, data={"a": "1"})


def test_cookies_are_read_only_and_exclusive_with_a_cookie_header() -> None:
    request = Request(URL, cookies={"session": "abc"})

    assert dict(request.cookies) == {"session": "abc"}
    with pytest.raises(TypeError):
        request.cookies["session"] = "def"  # type: ignore[index]
    with pytest.raises(ValueError, match="either as cookies= or as a Cookie header"):
        Request(URL, cookies={"a": "1"}, headers={"cookie": "b=2"})
    assert Request(URL, headers={"Cookie": "b=2"}).headers.get("cookie") == "b=2"


def test_callbacks_must_be_methods() -> None:
    shop = Shop()

    assert Request(URL, callback=shop.parse_item, errback=shop.parse_item).callback == shop.parse_item
    with pytest.raises(TypeError, match="callback must be a method of the Spider"):
        Request(URL, callback=lambda response: None)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="errback must be a method of the Spider"):
        Request(URL, errback=print)  # type: ignore[arg-type]


@pytest.mark.parametrize("timeout", [0, -1.5])
def test_timeouts_must_be_positive(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout must be positive"):
        Request(URL, timeout=timeout)


def test_mappings_are_copied_and_read_only() -> None:
    kwargs = {"category": "books"}
    ext = {"retry": {"attempt": 1}}
    request = Request(URL, kwargs=kwargs, ext=ext)
    kwargs["category"] = "games"
    ext["retry"]["attempt"] = 2

    assert dict(request.kwargs) == {"category": "books"}
    assert dict(request.ext["retry"]) == {"attempt": 1}
    with pytest.raises(TypeError):
        request.ext["retry"]["attempt"] = 3  # type: ignore[index]


def test_replace_accepts_only_fields_and_revalidates() -> None:
    request = Request(URL, method="POST", json={"a": 1}, cookies={"a": "1"}, priority=5)

    copy = request.replace(priority=9, ext={"retry": {"attempt": 1}})
    assert (copy.body, copy.headers, copy.cookies, copy.priority) == (
        request.body,
        request.headers,
        request.cookies,
        9,
    )
    with pytest.raises(TypeError, match=r"unknown fields \['json'\]"):
        request.replace(json={"b": 2})
    with pytest.raises(ValueError, match="not both"):
        request.replace(headers={"Cookie": "b=2"})


@pytest.mark.parametrize("url", ["/catalogue/page-2.html", "page-2.html", "//cdn.example.com/a", "?page=2", ""])
def test_relative_urls_are_rejected_outside_a_callback(url: str) -> None:
    with pytest.raises(ValueError, match="relative URL; Requests constructed outside a Callback"):
        Request(url)


def test_relative_urls_resolve_while_a_resolver_is_in_effect() -> None:
    with resolve_relative_urls(lambda url: f"https://example.com/base/{url}"):
        request = Request(" page-2.html\n", callback=Shop().parse_item)
        absolute = Request("https://other.example/")

    assert request.url == "https://example.com/base/page-2.html"
    assert absolute.url == "https://other.example/"
    with pytest.raises(ValueError, match="relative URL"):
        Request("page-3.html")


def test_urls_are_cleaned_as_browsers_do() -> None:
    assert Request("  https://example.com/a\n/b\t?q=1 ").url == "https://example.com/a/b?q=1"
