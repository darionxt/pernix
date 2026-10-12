from __future__ import annotations

import pytest

from pernix.urls import clean_url, document_base, is_absolute, join_url, normalize_url


def test_clean_url_removes_what_browsers_ignore() -> None:
    assert clean_url("\x00 \thttps://example.com/a\n/b\r\n?q=1 \x1f") == "https://example.com/a/b?q=1"
    assert clean_url("a b") == "a b"


@pytest.mark.parametrize(
    ("url", "absolute"),
    [
        ("https://example.com/", True),
        ("data:text/plain,hi", True),
        ("//example.com/", False),
        ("/path", False),
        ("page.html", False),
        ("", False),
    ],
)
def test_is_absolute_requires_a_scheme(url: str, absolute: bool) -> None:
    assert is_absolute(url) is absolute


def test_join_url_cleans_before_joining() -> None:
    assert join_url("https://example.com/a/b", " ../c\n.html ") == "https://example.com/c.html"


@pytest.mark.parametrize(
    ("href", "base"),
    [
        (None, "https://example.com/a/b.html"),
        ("/c/", "https://example.com/c/"),
        ("ftp://files.example.com/", "ftp://files.example.com/"),
        ("javascript:void(0)", "https://example.com/a/b.html"),
        ("data:text/html,x", "https://example.com/a/b.html"),
        ("mailto:a@example.com", "https://example.com/a/b.html"),
        ("http://[::1", "https://example.com/a/b.html"),
    ],
)
def test_document_base_ignores_what_browsers_ignore(href: str | None, base: str) -> None:
    assert document_base("https://example.com/a/b.html", href) == base


@pytest.mark.parametrize(
    ("url", "normalized"),
    [
        ("HTTPS://Example.COM:443/a/./b/../c", "https://example.com/a/c"),
        ("https://example.com", "https://example.com/"),
        ("https://example.com/page#reviews", "https://example.com/page"),
        ("https://example.com/?b=2&a=1&b=1&a", "https://example.com/?a=1&a&b=2&b=1"),
        ("https://example.com/%7Euser/%41?x=%41&a=%e4%b8%ad", "https://example.com/%7Euser/%41?a=%e4%b8%ad&x=%41"),
        ("https://例子.测试/路径?q=a b", "https://xn--fsqu00a.xn--0zwm56d/%E8%B7%AF%E5%BE%84?q=a%20b"),
        ("https://example.com/path/", "https://example.com/path/"),
        ("http://[", "http://["),
    ],
)
def test_urls_are_serialized_by_whatwg_rules_without_fragments_with_sorted_queries(url: str, normalized: str) -> None:
    assert normalize_url(url) == normalized


def test_query_sorting_can_be_turned_off() -> None:
    assert normalize_url("https://example.com/?b=2&a=1#x", sort_query=False) == "https://example.com/?b=2&a=1"
