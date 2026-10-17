from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Cookie, CookieJar
from pernix.fetchers.browser_cookies import (
    apply_page_changes,
    browser_cookie,
    browser_key,
    changes,
    keyed,
    request_cookie,
)

if TYPE_CHECKING:
    from patchright._impl._api_structures import Cookie as BrowserCookie

NOW = 1_800_000_000.0


def _reported(**fields: Any) -> BrowserCookie:
    """A cookie as the browser reports it, with its defaults."""
    cookie: dict[str, Any] = {
        "name": "a",
        "value": "1",
        "domain": "shop.example",
        "path": "/",
        "expires": -1,
        "httpOnly": False,
        "secure": False,
        "sameSite": "Lax",
    }
    cookie.update(fields)
    return cookie  # type: ignore[return-value]


@pytest.mark.parametrize(
    ("cookie", "written"),
    [
        (
            Cookie(name="a", value="1", domain="shop.example"),
            {"name": "a", "value": "1", "domain": "shop.example", "path": "/", "httpOnly": False, "secure": False},
        ),
        (
            Cookie(
                name="a",
                value="1",
                domain="shop.example",
                host_only=False,
                path="/cart",
                expires=NOW,
                secure=True,
                http_only=True,
                same_site="strict",
            ),
            {
                "name": "a",
                "value": "1",
                "domain": ".shop.example",
                "path": "/cart",
                "httpOnly": True,
                "secure": True,
                "expires": NOW,
                "sameSite": "Strict",
            },
        ),
        (
            Cookie(name="a", value="1", domain="::1"),
            {"name": "a", "value": "1", "domain": "[::1]", "path": "/", "httpOnly": False, "secure": False},
        ),
    ],
    ids=["host-only", "every-attribute", "ipv6"],
)
def test_a_jar_cookie_is_written_as_the_browser_takes_it(cookie: Cookie, written: dict[str, Any]) -> None:
    assert browser_cookie(cookie) == written


def test_a_requests_own_cookie_is_host_only_and_secure_only_with_a_prefix() -> None:
    assert request_cookie("shop.example", "a", "1") == {
        "name": "a",
        "value": "1",
        "domain": "shop.example",
        "path": "/",
        "secure": False,
    }
    assert request_cookie("shop.example", "__Host-id", "1").get("secure") is True


def test_changes_add_what_the_browser_lacks_or_holds_otherwise_and_expire_the_rest() -> None:
    same = browser_cookie(Cookie(name="same", value="1", domain="shop.example"))
    other = browser_cookie(Cookie(name="other", value="new", domain="shop.example"))
    new = browser_cookie(Cookie(name="new", value="1", domain="shop.example", expires=NOW))
    held = keyed(
        [
            _reported(name="same"),
            _reported(name="other", value="old"),
            _reported(name="extra", secure=True),
            _reported(name="third-party", partitionKey="https://site.example"),  # left alone
        ]
    )

    wanted = {browser_key(cookie): cookie for cookie in (same, other, new)}

    assert changes(held, wanted) == [
        other,
        new,
        {
            "name": "extra",
            "value": "",
            "domain": "shop.example",
            "path": "/",
            "secure": True,
            "httpOnly": False,
            "expires": 0,
        },
    ]
    assert (
        changes(
            keyed([_reported(name="dated", expires=NOW + 0.4)]),
            {
                ("dated", "shop.example", "/"): browser_cookie(
                    Cookie(name="dated", value="1", domain="shop.example", expires=NOW)
                )
            },
        )
        == []
    )  # the browser rounds an expiry


def test_page_changes_keep_what_the_browser_does_not_tell() -> None:
    jar = CookieJar(
        [
            Cookie(name="a", value="1", domain="shop.example", created=NOW - 100, modified=NOW - 100),
            Cookie(name="kept", value="1", domain="shop.example", created=NOW - 100, modified=NOW - 100),
        ]
    )
    before = keyed([_reported(name="a"), _reported(name="kept")])

    apply_page_changes(jar, before, keyed([_reported(name="a", value="2"), _reported(name="kept")]), now=NOW)

    changed, kept = list(jar)
    assert (changed.value, changed.same_site, changed.created, changed.modified) == ("2", None, NOW - 100, NOW)
    assert kept.modified == NOW - 100  # left as it was


def test_page_changes_take_the_attributes_the_browser_says_changed() -> None:
    jar = CookieJar([Cookie(name="a", value="1", domain="shop.example", created=NOW - 100)])
    before = keyed([_reported()])
    after = keyed([_reported(sameSite="Strict", secure=True, httpOnly=True, expires=NOW + 60)])

    apply_page_changes(jar, before, after, now=NOW)

    [cookie] = jar
    assert (cookie.same_site, cookie.secure, cookie.http_only, cookie.expires) == ("strict", True, True, NOW + 60)


def test_a_cookie_the_page_made_is_kept_as_the_browser_reports_it() -> None:
    jar = CookieJar()

    apply_page_changes(
        jar, {}, keyed([_reported(name="made", domain=".shop.example", path="/cart", expires=NOW + 60)]), now=NOW
    )

    assert list(jar) == [
        Cookie(
            name="made",
            value="1",
            domain="shop.example",
            host_only=False,
            path="/cart",
            expires=NOW + 60,
            same_site="lax",  # what the browser reports for one without SameSite
            created=NOW,
            modified=NOW,
        )
    ]


def test_a_cookie_the_page_removed_is_removed_and_timed() -> None:
    jar = CookieJar([Cookie(name="a", value="1", domain="shop.example")])

    apply_page_changes(jar, keyed([_reported(), _reported(name="own")]), {}, now=NOW)

    assert list(jar) == []
    assert jar.removals == {("shop.example", True, "/", "a"): NOW, ("shop.example", True, "/", "own"): NOW}


def test_cookies_the_page_left_alone_change_nothing() -> None:
    jar = CookieJar()
    own = keyed([_reported(name="own")])  # a Request's own cookie, not in the jar

    apply_page_changes(jar, own, own, now=NOW)

    assert (list(jar), jar.removals) == ([], {})
