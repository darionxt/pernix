"""The cookies one HTTP fetch sends to each hop of its redirect chain."""

from __future__ import annotations

from pernix import Cookie, Request, Session
from pernix.fetchers.chain_cookies import ChainCookies


def test_the_requests_cookies_go_to_its_host_only_in_the_order_given() -> None:
    cookies = ChainCookies(Request("http://app.test/login", cookies={"session": "1", "a": "2", "lang": "3"}), None)

    assert cookies.header("http://app.test:8080/deep/page") == "session=1; a=2; lang=3"
    assert cookies.header("https://app.test/") == "session=1; a=2; lang=3"
    assert cookies.header("http://www.app.test/") is None


def test_a_cookie_header_is_taken_as_the_requests_cookies() -> None:
    cookies = ChainCookies(Request("http://app.test/", headers={"Cookie": "b=2; a=1"}), None)

    assert cookies.header("http://app.test/") == "b=2; a=1"


def test_cookies_set_on_the_way_join_in_browser_order() -> None:
    cookies = ChainCookies(Request("http://app.test/start", cookies={"a": "1", "b": "2"}), None)

    cookies.receive("http://app.test/start", ["a=new", "deep=3; Path=/end", "c=4", "b=; Max-Age=0"])

    assert cookies.header("http://app.test/end") == "deep=3; a=new; c=4"  # the replaced cookie in its place


def test_the_requests_cookies_replace_the_sessions_in_their_place_and_are_never_kept() -> None:
    session = Session(
        cookies=[
            Cookie(name="first", value="1", domain="app.test", created=1.0),
            Cookie(name="lang", value="fr", domain="app.test", created=2.0),
            Cookie(name="last", value="1", domain="app.test", created=3.0),
            Cookie(name="elsewhere", value="1", domain="app.test", path="/other", created=4.0),
        ]
    )
    cookies = ChainCookies(Request("http://app.test/", cookies={"extra": "1", "lang": "en"}), session)

    cookies.receive("http://app.test/", ["set=1"])

    assert cookies.header("http://app.test/") == "first=1; lang=en; last=1; extra=1; set=1"
    assert [(cookie.name, cookie.value) for cookie in session.cookies] == [
        ("first", "1"),
        ("lang", "fr"),
        ("last", "1"),
        ("elsewhere", "1"),
        ("set", "1"),
    ]


def test_cookies_a_header_cannot_carry_are_left_out() -> None:
    session = Session(
        cookies=[
            Cookie(name="line", value="a\r\nX-Injected: 1", domain="app.test"),
            Cookie(name="tab", value="a\tb", domain="app.test"),
        ]
    )

    assert ChainCookies(Request("http://app.test/"), session).header("http://app.test/") == "tab=a\tb"


def test_a_url_naming_no_host_holds_no_cookies() -> None:
    cookies = ChainCookies(Request("http:///path", cookies={"a": "1"}), None)

    assert cookies.header("http:///path") is None
