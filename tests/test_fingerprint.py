from __future__ import annotations

import pytest

from pernix import Request
from pernix.fingerprint import request_fingerprint

URL = "https://example.com/search?q=books"


def test_fingerprints_are_short_and_stable() -> None:
    fingerprint = request_fingerprint(Request(URL))

    assert len(fingerprint) == 32
    assert int(fingerprint, 16) >= 0
    assert request_fingerprint(Request(URL)) == fingerprint


@pytest.mark.parametrize(
    "variant",
    [
        Request("https://EXAMPLE.com/search?q=books#top"),
        Request(URL, cookies={"session": "a"}),
        Request(URL, headers={"Cookie": "session=b"}),
        Request(URL, headers={"Accept-Language": "en"}),
        Request(URL, render=True, fetcher="browser", session="account-1"),
        Request(URL, meta={"page": 1}, priority=5),
    ],
)
def test_what_does_not_make_a_request_new(variant: Request) -> None:
    assert request_fingerprint(variant) == request_fingerprint(Request(URL))


@pytest.mark.parametrize(
    "variant",
    [
        Request("https://example.com/search?q=games"),
        Request(URL, method="POST"),
        Request(URL, method="POST", body=b"page=2"),
        Request("http://example.com/search?q=books"),
    ],
)
def test_what_makes_a_request_new(variant: Request) -> None:
    assert request_fingerprint(variant) != request_fingerprint(Request(URL))


def test_bodies_compare_by_content() -> None:
    by_json = Request(URL, method="POST", json={"page": 2})
    by_bytes = Request(URL, method="POST", body=b'{"page":2}')

    assert request_fingerprint(by_json) == request_fingerprint(by_bytes)
    assert request_fingerprint(Request(URL, method="GET", body=b"")) == request_fingerprint(Request(URL))


def test_declared_headers_take_part() -> None:
    english = Request(URL, headers={"Accept-Language": "en"})
    german = Request(URL, headers={"accept-language": "de"})

    assert request_fingerprint(english, headers=["Accept-Language"]) != request_fingerprint(
        german, headers=["ACCEPT-LANGUAGE"]
    )
    assert request_fingerprint(english, headers=["X-Absent"]) == request_fingerprint(Request(URL), headers=["x-absent"])


def test_the_cookie_header_cannot_be_declared() -> None:
    with pytest.raises(ValueError, match="cookies never take part in a Fingerprint"):
        request_fingerprint(Request(URL), headers=[" cookie "])
