from __future__ import annotations

from typing import Any

import pytest

from pernix import Headers, Request, Response
from pernix.response import Redirect

URL = "https://example.com/catalogue/index.html"


def _response(body: bytes | str = b"", *, content_type: str | None = None, **kwargs: Any) -> Response:
    headers = [] if content_type is None else [("Content-Type", content_type)]
    return Response(
        url=URL,
        status=200,
        headers=headers,
        body=body.encode() if isinstance(body, str) else body,
        request=Request(URL),
        **kwargs,
    )


def test_responses_are_immutable() -> None:
    response = _response("<p>a</p>")

    with pytest.raises(AttributeError):
        response.status = 404  # type: ignore[misc]
    with pytest.raises(TypeError):
        response.cookies["a"] = "1"  # type: ignore[index]


def test_metadata_defaults() -> None:
    response = _response()

    assert (response.history, response.elapsed, response.ip_address) == ((), None, None)
    assert (response.http_version, response.fetcher, response.session) == (None, None, None)
    assert dict(response.cookies) == {}


def test_replace_keeps_the_rest_and_starts_fresh_caches() -> None:
    hop = Redirect("https://example.com/old", 301, Headers({"Location": URL}))
    response = _response('{"a": 1}', history=[hop], elapsed=0.5, fetcher="http", cookies={"s": "1"})
    assert response.json() == {"a": 1}

    copy = response.replace(body=b'{"a": 2}')

    assert copy.json() == {"a": 2}
    assert (copy.history, copy.elapsed, copy.fetcher, dict(copy.cookies)) == ((hop,), 0.5, "http", {"s": "1"})
    assert response.json() == {"a": 1}
    with pytest.raises(TypeError, match=r"unknown arguments \['text'\]"):
        response.replace(text="x")


def test_text_records_the_encoding_and_how_it_was_chosen() -> None:
    response = _response("<p>价格</p>".encode("gbk"), content_type="text/html; charset=gb2312")

    assert (response.text, response.encoding, response.encoding_source) == ("<p>价格</p>", "gb18030", "header")


def test_replace_with_an_encoding_decodes_again() -> None:
    response = _response("<p>价格</p>".encode("gbk"), content_type="text/html; charset=utf-8")

    fixed = response.replace(encoding="gbk")

    assert (fixed.text, fixed.encoding, fixed.encoding_source) == ("<p>价格</p>", "gb18030", "explicit")


def test_the_default_encoding_is_configurable() -> None:
    body = ("<html><body>" + "中文内容\uff0c这是一个测试。" * 20 + "</body></html>").encode()
    body = body[:100] + b"\xff" + body[100:]  # not UTF-8, and too little off to detect anything

    response = _response(body, default_encoding="gb18030")

    assert (response.encoding, response.encoding_source) == ("gb18030", "default")


@pytest.mark.parametrize("argument", ["encoding", "default_encoding"])
def test_unknown_encodings_are_rejected_at_construction(argument: str) -> None:
    with pytest.raises(LookupError):
        _response(**{argument: "no-such-encoding"})


def test_json_is_decoded_once_from_the_text() -> None:
    response = _response('{"名": [1, 2]}'.encode("gbk"), content_type="application/json; charset=gbk")

    first = response.json()

    assert first == {"名": [1, 2]}
    assert response.json() is first


def test_invalid_json_raises_value_error() -> None:
    with pytest.raises(ValueError, match="unexpected"):
        _response("<html>").json()


def test_css_and_xpath_select_from_html() -> None:
    response = _response("<ul><li class='a'>1</li><li>2</li></ul>", content_type="text/html")

    assert response.css("li.a::text").get() == "1"
    assert response.xpath("//li[$n]/text()", n=2).get() == "2"


def test_re_searches_the_whole_text() -> None:
    response = _response("<script>var price = 12.50; var id = 'b-7';</script>", content_type="text/html")

    assert response.re(r"price = ([\d.]+)") == ["12.50"]
    assert response.re(r"(?P<extract>\w)-(\d)") == ["b"]
    assert response.re_first(r"id = '([^']+)'") == "b-7"
    assert response.re_first(r"missing (\d+)") is None
    assert response.re_first(r"missing (\d+)", "0") == "0"


def test_re_keeps_entities_on_request() -> None:
    response = _response("<p>a &gt; b</p>", content_type="text/html")

    assert response.re(r"<p>(.*)</p>") == ["a > b"]
    assert response.re(r"<p>(.*)</p>", replace_entities=False) == ["a &gt; b"]


def test_jmespath_returns_json_values_as_they_are() -> None:
    response = _response('{"data": {"total": 2, "items": [{"id": 7}, {"id": 8}]}}', content_type="application/json")

    assert response.jmespath("data.total").get() == 2
    assert response.jmespath("data.items[*].id").getall() == [7, 8]
    assert response.selector.type == "json"


def test_json_mislabelled_as_html_is_still_json() -> None:
    response = _response('[{"name": "a"}]', content_type="text/html; charset=utf-8")

    assert response.jmespath("[0].name").get() == "a"


def test_json_embedded_in_html_is_queried_through_a_selection() -> None:
    response = _response('<script id="data">{"props": {"title": "T"}}</script>', content_type="text/html")

    assert response.jmespath("props.title").get() is None
    assert response.css("script#data::text").jmespath("props.title").get() == "T"


def test_invalid_json_labelled_as_json_falls_back_to_html() -> None:
    response = _response("<p>oops</p>", content_type="application/json")

    assert response.css("p::text").get() == "oops"


def test_xml_media_types_are_parsed_as_xml() -> None:
    body = "<urlset><URL><loc>https://example.com/a</loc></URL></urlset>"

    as_xml = _response(body, content_type="application/xml")
    as_html = _response(body, content_type="text/html")

    assert as_xml.selector.type == "xml"
    assert as_xml.xpath("//URL/loc/text()").get() == "https://example.com/a"
    assert as_html.xpath("//URL/loc/text()").get() is None, "HTML parsing lower-cases element names"


def test_urljoin_uses_the_final_url() -> None:
    response = _response("<a href='x'>x</a>", content_type="text/html")

    assert response.urljoin("page-2.html") == "https://example.com/catalogue/page-2.html"
    assert response.urljoin("../book/1.html") == "https://example.com/book/1.html"
    assert response.urljoin("//cdn.example.com/a.png") == "https://cdn.example.com/a.png"
    assert response.urljoin("https://other.example/") == "https://other.example/"
    assert response.urljoin("\n  page-\n2.html  ") == "https://example.com/catalogue/page-2.html"


@pytest.mark.parametrize(
    ("base", "expected"),
    [
        ("/mirror/", "https://example.com/mirror/page-2.html"),
        ("https://static.example.com/books/", "https://static.example.com/books/page-2.html"),
        ("", "https://example.com/catalogue/page-2.html"),
    ],
)
def test_urljoin_honors_the_first_base_href(base: str, expected: str) -> None:
    html = f'<html><head><base target="_top"><base href="{base}"><base href="/ignored/"></head></html>'

    assert _response(html, content_type="text/html").urljoin("page-2.html") == expected


def test_urljoin_ignores_base_elements_outside_html() -> None:
    response = _response('{"base": "<base href=\\"/x/\\">"}', content_type="application/json")

    assert response.urljoin("a") == "https://example.com/catalogue/a"
