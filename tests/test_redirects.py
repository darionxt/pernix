"""The rules the HTTP Fetcher follows redirects by: where they lead, and how the request changes."""

from __future__ import annotations

import pytest
from curl_cffi import CurlECode

from pernix.errors import FetchError
from pernix.fetchers.redirects import Hop, redirect_target, redirected
from pernix.headers import Headers

BASE = "http://example.test/a/b?q=1"


@pytest.mark.parametrize(
    ("location", "target"),
    [
        ("/c", "http://example.test/c"),
        ("c", "http://example.test/a/c"),
        ("?r=2", "http://example.test/a/b?r=2"),
        ("//other.test/d", "http://other.test/d"),
        ("https://other.test:443/e", "https://other.test/e"),
        ("http:///192.0.2.1/page", "http://192.0.2.1/page"),  # where browsers, not urllib, go
        ("http:\\\\192.0.2.1/page", "http://192.0.2.1/page"),
        ("https://faß.example/", "https://xn--fa-hia.example/"),
        ("/caf%E9 x", "http://example.test/caf%E9%20x"),
    ],
)
def test_a_location_is_resolved_as_browsers_resolve_it(location: str, target: str) -> None:
    assert redirect_target(BASE, 302, location) == target


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirect_statuses_with_a_location_redirect(status: int) -> None:
    assert redirect_target(BASE, status, "/next") == "http://example.test/next"


@pytest.mark.parametrize(("status", "location"), [(200, "/next"), (300, "/next"), (304, "/next"), (302, None)])
def test_other_responses_do_not_redirect(status: int, location: str | None) -> None:
    assert redirect_target(BASE, status, location) is None


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://[unclosed/", CurlECode.URL_MALFORMAT),
        ("http://a b/", CurlECode.URL_MALFORMAT),
        ("ftp://example.test/file", CurlECode.UNSUPPORTED_PROTOCOL),
        ("javascript:alert(1)", CurlECode.UNSUPPORTED_PROTOCOL),
        ("data:,hello", CurlECode.UNSUPPORTED_PROTOCOL),
    ],
)
def test_locations_browsers_refuse_to_follow_fail(location: str, code: int) -> None:
    with pytest.raises(FetchError, match=r"the redirect from http://example\.test/a/b\?q=1 leads to") as refused:
        redirect_target(BASE, 302, location)

    assert refused.value.code == code


HEADERS = Headers(
    [
        ("Content-Type", "text/plain"),
        ("Content-Encoding", "gzip"),
        ("Content-Language", "en"),
        ("Content-Location", "/form"),
        ("Authorization", "Bearer t"),
        ("Proxy-Authorization", "Basic p"),
        ("X-Other", "1"),
    ]
)
BODY_HEADERS = ("Content-Type", "Content-Encoding", "Content-Language", "Content-Location")


@pytest.mark.parametrize(
    ("status", "method", "followed_with"),
    [
        (301, "POST", "GET"),
        (302, "POST", "GET"),
        (303, "POST", "GET"),
        (303, "PUT", "GET"),
        (303, "PATCH", "GET"),
        (303, "GET", "GET"),
        (303, "HEAD", "HEAD"),
        (301, "PUT", "PUT"),
        (302, "PATCH", "PATCH"),
        (307, "POST", "POST"),
        (308, "POST", "POST"),
        (308, "DELETE", "DELETE"),
    ],
)
def test_the_method_and_body_change_as_the_fetch_standard_says(status: int, method: str, followed_with: str) -> None:
    hop = Hop(method, "http://example.test/form", HEADERS, b"a=1")

    followed = redirected(hop, status, "http://example.test/done")

    kept = followed_with == method
    assert (followed.method, followed.url, followed.body) == (
        followed_with,
        "http://example.test/done",
        b"a=1" if kept else None,
    )
    assert [followed.headers.get(name) for name in BODY_HEADERS] == [
        HEADERS.get(name) if kept else None for name in BODY_HEADERS
    ]
    assert (followed.headers.get("Authorization"), followed.headers.get("X-Other")) == ("Bearer t", "1")


@pytest.mark.parametrize(
    ("url", "target", "same_origin"),
    [
        ("http://example.test/a", "http://example.test/b", True),
        ("http://example.test/a", "http://example.test:80/b", True),
        ("https://example.test:443/a", "https://example.test/b", True),
        ("http://EXAMPLE.test/a", "http://example.test/b", True),
        ("http://faß.example/a", "http://xn--fa-hia.example/b", True),
        ("http://example.test/a", "https://example.test/b", False),
        ("http://example.test/a", "http://example.test:8080/b", False),
        ("http://example.test/a", "http://www.example.test/b", False),
        ("http://example.test:99999/a", "http://example.test/b", False),  # a port no URL has
    ],
)
def test_credentials_are_dropped_on_the_way_to_another_origin(url: str, target: str, same_origin: bool) -> None:
    followed = redirected(Hop("GET", url, HEADERS, None), 307, target)

    credentials = [followed.headers.get(name) for name in ("Authorization", "Proxy-Authorization")]
    assert credentials == (["Bearer t", "Basic p"] if same_origin else [None, None])
    assert [name for name, _ in followed.headers if name not in ("Authorization", "Proxy-Authorization")] == [
        *BODY_HEADERS,
        "X-Other",
    ]


def test_dropped_headers_stay_dropped_on_later_hops() -> None:
    first = Hop("POST", "http://example.test/form", HEADERS, b"a=1")

    away = redirected(first, 302, "http://other.test/")
    back = redirected(away, 307, "http://example.test/again")

    assert (back.method, back.body) == ("GET", None)
    assert list(back.headers) == [("X-Other", "1")]
