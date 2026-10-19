"""The headers Chrome sends for each kind of request, as the HTTP Fetcher makes them with the pinned Profile."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.fetchers.chrome_headers import ChromeRequest, chrome_request, site_relation
from pernix.fetchers.pinned_pair import client_headers

if TYPE_CHECKING:
    from pernix.request_kinds import RequestKind

CLIENT = client_headers(["en-US", "en"], platform="Linux")
PAGE = "https://shop.example/books/1?tab=2#reviews"


def _made(
    kind: RequestKind,
    url: str | list[str],
    *,
    method: str = "GET",
    referrer: str | None = PAGE,
    own: list[tuple[str, str]] | None = None,
) -> ChromeRequest:
    chain = [url] if isinstance(url, str) else url
    return chrome_request(kind, chain=chain, method=method, own=own or [], referrer=referrer, client=CLIENT)


def _headers(made: ChromeRequest) -> dict[str, str]:
    return {name.lower(): value for name, value in made.headers}


def test_a_navigation_typed_in_is_chromes_with_no_page() -> None:
    made = _made("navigation", "https://shop.example/", referrer=None)

    assert [name for name, _ in made.headers] == [
        "Connection",
        "sec-ch-ua",
        "sec-ch-ua-mobile",
        "sec-ch-ua-platform",
        "Upgrade-Insecure-Requests",
        "User-Agent",
        "Accept",
        "Sec-Fetch-Site",
        "Sec-Fetch-Mode",
        "Sec-Fetch-User",
        "Sec-Fetch-Dest",
        "Accept-Encoding",
        "Accept-Language",
        "Priority",
    ]
    assert _headers(made)["sec-fetch-site"] == "none"
    assert (made.weight, made.credentials) == (256, True)
    assert made.order.startswith("host,connection,sec-ch-ua,")
    assert made.order.endswith(",accept-language,cookie,priority")


def test_a_post_of_a_fetch_puts_its_body_headers_where_chrome_does() -> None:
    own = [("Content-Type", "application/json")]
    made = _made("fetch", "https://shop.example/api", method="POST", own=own)

    assert made.order.split(",")[:8] == [
        "host",
        "connection",
        "content-length",
        "sec-ch-ua-platform",
        "user-agent",
        "sec-ch-ua",
        "content-type",
        "sec-ch-ua-mobile",
    ]
    headers = _headers(made)
    assert (headers["origin"], headers["sec-fetch-mode"], headers["sec-fetch-dest"]) == (
        "https://shop.example",
        "cors",
        "empty",
    )
    assert (headers["priority"], made.weight) == ("u=1, i", 220)


@pytest.mark.parametrize(
    ("kind", "accept", "mode", "dest", "priority", "weight"),
    [
        ("iframe", "text/html,", "navigate", "iframe", "u=0, i", 256),
        ("style", "text/css,*/*;q=0.1", "no-cors", "style", "u=0", 256),
        ("script", "*/*", "no-cors", "script", "u=1", 220),
        ("image", "image/avif,", "no-cors", "image", "u=2, i", 183),
    ],
)
def test_frames_and_loads_have_their_own_accept_mode_destination_and_priority(
    kind: RequestKind, accept: str, mode: str, dest: str, priority: str, weight: int
) -> None:
    made = _made(kind, "https://shop.example/thing")

    headers = _headers(made)
    assert headers["accept"].startswith(accept)
    assert (headers["sec-fetch-mode"], headers["sec-fetch-dest"], headers["priority"]) == (mode, dest, priority)
    assert made.weight == weight
    assert "sec-fetch-user" not in headers
    assert "origin" not in headers


@pytest.mark.parametrize(
    ("url", "referer"),
    [
        ("https://shop.example/next", "https://shop.example/books/1?tab=2"),
        ("https://cdn.example/a.png", "https://shop.example/"),
        ("http://shop.example/next", None),
    ],
    ids=["the same origin", "another origin", "a downgrade"],
)
def test_the_referer_is_the_page_by_strict_origin_when_cross_origin(url: str, referer: str | None) -> None:
    assert _headers(_made("navigation", url)).get("referer") == referer


def test_credentials_in_the_page_are_never_sent() -> None:
    made = _made("navigation", "https://shop.example/next", referrer="https://user:secret@shop.example/a#b")

    assert _headers(made)["referer"] == "https://shop.example/a"


@pytest.mark.parametrize(
    ("initiator", "url", "relation"),
    [
        (None, "https://shop.example/", "none"),
        ("https://shop.example", "https://shop.example/a", "same-origin"),
        ("https://shop.example", "https://api.shop.example/a", "same-site"),
        ("https://shop.example", "http://shop.example/a", "cross-site"),
        ("https://shop.example", "https://other.example/a", "cross-site"),
        ("https://127.0.0.1:8443", "https://127.0.0.1:9443/", "same-site"),
        ("https://127.0.0.1", "https://localhost/", "cross-site"),
    ],
)
def test_sec_fetch_site_relates_the_url_to_the_page(initiator: str | None, url: str, relation: str) -> None:
    assert site_relation(initiator, url) == relation


def test_a_redirect_chain_through_another_site_stays_cross_site() -> None:
    chain = ["https://shop.example/a", "https://other.example/b", "https://shop.example/c"]

    assert _headers(_made("navigation", chain))["sec-fetch-site"] == "cross-site"


@pytest.mark.parametrize(
    ("kind", "method", "url", "origin"),
    [
        ("navigation", "GET", "https://shop.example/next", None),
        ("form", "POST", "https://shop.example/submit", "https://shop.example"),
        ("fetch", "GET", "https://shop.example/api", None),
        ("fetch", "GET", "https://api.example/v1", "https://shop.example"),
        ("fetch", "DELETE", "https://shop.example/api", "https://shop.example"),
        ("image", "GET", "https://cdn.example/a.png", None),
    ],
)
def test_the_origin_goes_with_forms_and_with_fetches_but_same_origin_gets(
    kind: RequestKind, method: str, url: str, origin: str | None
) -> None:
    assert _headers(_made(kind, url, method=method)).get("origin") == origin


def test_a_redirect_from_another_origin_to_yet_another_taints_the_origin() -> None:
    chain = ["https://api.example/v1", "https://cdn.example/v1"]

    assert _headers(_made("fetch", chain, method="POST"))["origin"] == "null"


def test_a_form_redirected_to_a_get_is_a_navigation() -> None:
    made = _made("form", ["https://shop.example/submit", "https://shop.example/done"], method="GET")

    headers = _headers(made)
    assert "origin" not in headers
    assert "cache-control" not in headers
    assert headers["sec-fetch-user"] == "?1"


def test_a_request_a_page_makes_without_a_referer_comes_from_its_own_origin() -> None:
    made = _made("fetch", "https://shop.example/api", method="POST", referrer=None)

    headers = _headers(made)
    assert (headers["sec-fetch-site"], headers["origin"]) == ("same-origin", "https://shop.example")
    assert "referer" not in headers
    assert made.credentials


def test_a_fetch_to_another_origin_takes_no_cookies() -> None:
    assert not _made("fetch", "https://api.shop.example/v1").credentials
    assert _made("image", "https://cdn.example/a.png").credentials


@pytest.mark.parametrize(
    ("kind", "active"), [("image", True), ("iframe", True), ("navigation", False), ("fetch", False)]
)
def test_loads_and_frames_of_another_site_tell_their_storage_access(kind: RequestKind, active: bool) -> None:
    headers = _headers(_made(kind, "https://other.example/thing"))

    assert ("sec-fetch-storage-access" in headers) == active


def test_an_insecure_origin_gets_no_client_hints_nor_fetch_metadata() -> None:
    made = _made("fetch", "http://shop.example/api", referrer="http://shop.example/page")

    assert [name for name, _ in made.headers] == [
        "Connection",
        "User-Agent",
        "Accept",
        "Referer",
        "Accept-Encoding",
        "Accept-Language",
        "Priority",
    ]
    assert _headers(made)["accept-encoding"] == "gzip, deflate"


def test_a_loopback_host_is_potentially_trustworthy_over_http() -> None:
    headers = _headers(_made("navigation", "http://localhost:8080/", referrer=None))

    assert headers["sec-fetch-site"] == "none"
    assert headers["accept-encoding"] == "gzip, deflate, br, zstd"


def test_the_requests_own_headers_replace_the_kinds_in_place_and_others_go_after_a_navigations() -> None:
    own = [("accept", "text/plain"), ("X-Trace", "1"), ("Referer", "https://elsewhere.example/"), ("X-Debug", "2")]
    made = _made("navigation", "https://shop.example/next", own=own)

    names = [name for name, _ in made.headers]
    assert names[names.index("User-Agent") + 1] == "accept"
    assert names[-3:] == ["Priority", "X-Trace", "X-Debug"]
    assert _headers(made)["referer"] == "https://shop.example/books/1?tab=2"
    assert made.order.endswith(",priority,x-trace,x-debug")


def test_the_requests_own_headers_go_first_among_those_a_page_adds() -> None:
    made = _made("fetch", "https://shop.example/api", own=[("X-Trace", "1"), ("Sec-Fetch-Dest", "empty")])

    names = [name for name, _ in made.headers]
    assert names[:3] == ["Connection", "X-Trace", "sec-ch-ua-platform"]
    assert names.count("Sec-Fetch-Dest") == 1


def test_a_fetch_back_from_another_origin_takes_no_cookies_and_sends_a_null_origin() -> None:
    chain = ["https://shop.example/api", "https://api.example/v1", "https://shop.example/api/2"]

    made = _made("fetch", chain)

    assert not made.credentials
    assert _headers(made)["origin"] == "null"


def test_the_referer_stays_cut_once_a_redirect_cut_it() -> None:
    back = ["https://shop.example/a", "https://other.example/b", "https://shop.example/c"]
    secure_again = ["https://shop.example/a", "http://other.example/b", "https://shop.example/c"]

    assert _headers(_made("navigation", back))["referer"] == "https://shop.example/"
    assert "referer" not in _headers(_made("navigation", secure_again))


def test_a_form_without_a_page_redirected_to_a_get_stays_on_its_own_origin() -> None:
    made = _made("form", ["https://shop.example/submit", "https://shop.example/done"], referrer=None)

    assert _headers(made)["sec-fetch-site"] == "same-origin"


def test_a_form_from_an_https_page_to_another_scheme_sends_a_null_origin() -> None:
    made = _made("form", "http://shop.example/submit", method="POST")

    assert _headers(made)["origin"] == "null"
