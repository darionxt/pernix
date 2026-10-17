import datetime as dt
import logging

import pytest

from pernix.cookies import (
    SetCookie,
    check_cookies,
    merge_set_cookies,
    parse_cookie_header,
    parse_set_cookie,
    requires_secure,
)

NOW = dt.datetime(2026, 10, 12, 12, 0, tzinfo=dt.UTC)


def test_set_cookie_headers_apply_in_order_on_top_of_the_held_cookies() -> None:
    held = {"lang": "en", "theme": "light"}

    merged = merge_set_cookies(held, ["theme=dark; Path=/", "auth=ok; HttpOnly", "auth=renewed"], now=NOW)

    assert merged == {"lang": "en", "theme": "dark", "auth": "renewed"}
    assert held == {"lang": "en", "theme": "light"}


def test_expired_cookies_are_removed() -> None:
    held = {"a": "1", "b": "2", "c": "3", "d": "4"}
    headers = [
        "a=; Max-Age=0",
        "b=gone; Max-Age=-1",
        "c=gone; Expires=Sun, 11 Oct 2026 12:00:00 GMT",
        "d=kept; Expires=Tue, 13 Oct 2026 12:00:00 GMT",
    ]

    assert merge_set_cookies(held, headers, now=NOW) == {"d": "kept"}


@pytest.mark.parametrize(
    "header",
    [
        "auth=ok; Partitioned; Secure; Path=/",
        "auth=ok; Priority=High",
        "auth=ok; SameSite=None; Secure; Domain=example.com; HttpOnly",
    ],
)
def test_unknown_attributes_are_ignored(header: str) -> None:
    assert parse_set_cookie(header) == SetCookie("auth", "ok")
    assert merge_set_cookies({}, [header], now=NOW) == {"auth": "ok"}


def test_attributes_after_unknown_ones_still_apply() -> None:
    assert merge_set_cookies({"auth": "old"}, ["auth=ok; Priority=High; Max-Age=0"], now=NOW) == {}


def test_max_age_takes_precedence_over_expires_and_the_last_valid_value_wins() -> None:
    cookie = parse_set_cookie("a=1; Expires=Sun, 11 Oct 2026 12:00:00 GMT; Max-Age=60; Max-Age=soon")

    assert cookie is not None
    assert cookie.max_age == 60
    assert not cookie.expired(NOW)


def test_values_are_kept_exactly_as_sent() -> None:
    assert parse_set_cookie('token="a b=c"; Path=/') == SetCookie("token", '"a b=c"')
    assert parse_set_cookie("empty=") == SetCookie("empty", "")


@pytest.mark.parametrize("header", ["no-pair", "=value", "; Path=/", ""])
def test_headers_without_a_named_pair_are_skipped(header: str) -> None:
    assert parse_set_cookie(header) is None
    assert merge_set_cookies({"a": "1"}, [header], now=NOW) == {"a": "1"}


def test_skipped_headers_are_logged_without_their_contents(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger="pernix.cookies"):
        merge_set_cookies({}, ["secret-token-without-equals"], now=NOW)

    assert caplog.records
    assert all("secret-token" not in record.getMessage() for record in caplog.records)


def test_cookie_headers_parse_into_cookies_by_name() -> None:
    assert parse_cookie_header('session=abc; lang=en;; broken; =x; quoted="v"') == {
        "session": "abc",
        "lang": "en",
        "quoted": '"v"',
    }


def test_oversized_max_age_values_decide_without_failing() -> None:
    huge = "9" * 5000

    kept = parse_set_cookie(f"a=1; Max-Age={huge}")
    removed = parse_set_cookie(f"b=2; Max-Age=-{huge}")

    assert kept is not None
    assert not kept.expired(NOW)
    assert removed is not None
    assert removed.expired(NOW)
    assert merge_set_cookies({"b": "old"}, [f"a=1; Max-Age={huge}", f"b=2; Max-Age=-{huge}"], now=NOW) == {"a": "1"}


@pytest.mark.parametrize(("max_age", "expired"), [("0" * 19, True), ("-0000", True), ("0005", False)])
def test_leading_zeros_do_not_change_the_max_age(max_age: str, expired: bool) -> None:
    cookie = parse_set_cookie(f"a=1; Max-Age={max_age}")

    assert cookie is not None
    assert cookie.expired(NOW) is expired
    assert merge_set_cookies({"a": "old"}, [f"a=1; Max-Age={max_age}"], now=NOW) == ({} if expired else {"a": "1"})


@pytest.mark.parametrize(("max_age", "seconds"), [("0" * 5000 + "5", 5), ("-" + "0" * 5000 + "5", -5)])
def test_long_zero_padded_max_age_values_convert_their_significant_digits(max_age: str, seconds: int) -> None:
    cookie = parse_set_cookie(f"a=1; Max-Age={max_age}")

    assert cookie is not None
    assert cookie.max_age == seconds


@pytest.mark.parametrize(
    ("name", "secure"),
    [
        ("__Secure-id", True),
        ("__Host-id", True),
        ("__secure-id", True),
        ("__HOST-id", True),
        ("id", False),
        ("_Secure-id", False),
    ],
)
def test_secure_only_prefixes_match_in_any_case(name: str, secure: bool) -> None:
    assert requires_secure(name) is secure


def test_cookies_a_cookie_header_carries_as_given_pass() -> None:
    check_cookies({"session": 'a b,c"d"', "__Host-id": "", "lang": "=en="})
