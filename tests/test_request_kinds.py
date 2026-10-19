"""The kind of request a Request is: from its Sec-Fetch-Dest, or from its method and body."""

from __future__ import annotations

import pytest

from pernix import Request
from pernix.blocked_responses import rendered_instead
from pernix.request_kinds import check_kind, referring_page, request_kind
from pernix.testing import make_response

URL = "https://shop.example/books"


@pytest.mark.parametrize(
    ("request_", "kind"),
    [
        (Request(URL), "navigation"),
        (Request(URL, data={"q": "dune"}), "form"),
        (Request(URL, body="a=1", headers={"Content-Type": "multipart/form-data; boundary=x"}), "form"),
        (Request(URL, body="a", headers={"content-type": "Text/Plain;charset=UTF-8"}), "form"),
        (Request(URL, json={"q": "dune"}), "fetch"),
        (Request(URL, body=b"\x00"), "fetch"),
        (Request(URL, method="PUT", data={"q": "dune"}), "fetch"),
        (Request(URL, method="HEAD"), "fetch"),
    ],
    ids=["GET", "urlencoded", "multipart", "text", "JSON", "bytes", "PUT", "HEAD"],
)
def test_the_kind_follows_the_method_and_the_body(request_: Request, kind: str) -> None:
    assert request_kind(request_) == kind


@pytest.mark.parametrize(
    ("destination", "method", "kind"),
    [
        ("document", "GET", "navigation"),
        ("document", "POST", "form"),
        ("Iframe", "GET", "iframe"),
        ("empty", "GET", "fetch"),
        ("empty", "POST", "fetch"),
        ("style", "GET", "style"),
        ("script", "GET", "script"),
        ("image", "GET", "image"),
    ],
)
def test_sec_fetch_dest_names_the_kind(destination: str, method: str, kind: str) -> None:
    request = Request(URL, method=method, headers={"Sec-Fetch-Dest": destination})

    assert request_kind(request) == kind


def test_a_destination_pernix_makes_no_request_for_is_refused() -> None:
    with pytest.raises(ValueError, match="Sec-Fetch-Dest: font names no kind of request pernix makes"):
        request_kind(Request(URL, headers={"Sec-Fetch-Dest": "font"}))


@pytest.mark.parametrize("destination", ["iframe", "style", "script", "image"])
def test_frames_and_loads_are_gets(destination: str) -> None:
    with pytest.raises(ValueError, match=f"Sec-Fetch-Dest: {destination} is a GET"):
        Request(URL, method="POST", headers={"Sec-Fetch-Dest": destination})


def test_only_navigations_render() -> None:
    with pytest.raises(ValueError, match="cannot have Sec-Fetch-Dest: empty"):
        Request(URL, render=True, headers={"Sec-Fetch-Dest": "empty"})

    check_kind("GET", Request(URL).headers, render=True)
    assert Request(URL, render=True, headers={"Sec-Fetch-Dest": "document"}).render


@pytest.mark.parametrize(
    ("request_", "page"),
    [
        (Request(URL), URL),
        (Request(URL, headers={"Sec-Fetch-Dest": "iframe", "Referer": "https://shop.example/"}), URL),
        (Request(URL, json={}, headers={"Referer": "https://shop.example/search"}), "https://shop.example/search"),
        (Request(URL, headers={"Sec-Fetch-Dest": "empty"}), None),
        (Request(URL, headers={"Sec-Fetch-Dest": "empty", "Referer": ""}), None),
        (Request(URL, json={}, render=True), URL),
    ],
    ids=["navigation", "iframe", "fetch", "fetch without a page", "fetch with an empty Referer", "rendered"],
)
def test_the_page_a_response_belongs_to_is_its_own_when_it_is_a_document(request_: Request, page: str | None) -> None:
    assert referring_page(make_response(URL, "{}", request=request_)) == page


@pytest.mark.parametrize(
    ("request_", "rendered"),
    [
        (Request(URL), True),
        (Request(URL, data={"q": "dune"}), True),
        (Request(URL, json={}), False),
        (Request(URL, headers={"Sec-Fetch-Dest": "image"}), False),
        (Request(URL, headers={"Sec-Fetch-Dest": "font"}), False),
    ],
    ids=["navigation", "form", "fetch", "image", "unknown"],
)
def test_only_navigations_found_blocked_are_rendered_instead(request_: Request, rendered: bool) -> None:
    assert (rendered_instead(request_) is not None) == rendered
