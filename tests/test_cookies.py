import datetime as dt

from pernix.cookies import merge_set_cookies

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


def test_unparsable_headers_are_skipped() -> None:
    assert merge_set_cookies({"a": "1"}, ["\x00broken", "b=2"], now=NOW) == {"a": "1", "b": "2"}
