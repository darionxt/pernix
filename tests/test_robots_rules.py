"""Reading a robots.txt, and what the answer to fetching one means, as RFC 9309 has it."""

from __future__ import annotations

import pytest

from pernix.robots import Access, RobotsRules, access, is_robots_txt

SITE = "https://site.example"

ROBOTS_TXT = b"""\
User-agent: *
Disallow: /private
Crawl-delay: 5

User-agent: ExampleBot
Disallow: /
Allow: /public
"""


@pytest.mark.parametrize(
    ("status", "meaning"),
    [
        (200, Access.SUCCESSFUL),
        (204, Access.SUCCESSFUL),
        (404, Access.UNAVAILABLE),
        (401, Access.UNAVAILABLE),
        (403, Access.UNAVAILABLE),
        (304, Access.UNAVAILABLE),
        (429, Access.UNREACHABLE),
        (500, Access.UNREACHABLE),
        (503, Access.UNREACHABLE),
    ],
)
def test_the_status_tells_whether_the_rules_apply_there_are_none_or_they_cannot_be_told(
    status: int, meaning: Access
) -> None:
    assert access(status) is meaning


def test_the_groups_naming_the_product_token_apply_or_else_the_wildcard_group() -> None:
    pernix = RobotsRules.parse(ROBOTS_TXT, "pernix")
    bot = RobotsRules.parse(ROBOTS_TXT, "examplebot")
    wildcard = RobotsRules.parse(ROBOTS_TXT, "*")

    assert (pernix.allows(f"{SITE}/private/a"), pernix.allows(f"{SITE}/page"), pernix.crawl_delay) == (False, True, 5)
    assert (bot.allows(f"{SITE}/page"), bot.allows(f"{SITE}/public/a"), bot.crawl_delay) == (False, True, None)
    assert (wildcard.allows(f"{SITE}/private"), wildcard.allows(f"{SITE}/page")) == (False, True)


def test_a_byte_order_mark_does_not_hide_the_first_group() -> None:
    rules = RobotsRules.parse(b"\xef\xbb\xbfUser-agent: *\nDisallow: /a\n", "pernix")

    assert not rules.allows(f"{SITE}/a")


def test_a_body_that_is_not_utf_8_is_read_with_its_bytes_replaced() -> None:
    rules = RobotsRules.parse(b"User-agent: *\nDisallow: /caf\xe9\nDisallow: /b\n", "pernix")

    assert not rules.allows(f"{SITE}/b")


def test_the_line_a_cut_body_ends_in_is_left_out() -> None:
    body = b"User-agent: *\nDisallow: /secret\nDisallow: /s"

    assert RobotsRules.parse(body, "pernix", cut=True).allows(f"{SITE}/shop")
    assert not RobotsRules.parse(body, "pernix", cut=True).allows(f"{SITE}/secret")
    assert not RobotsRules.parse(body, "pernix").allows(f"{SITE}/shop")


def test_rules_allow_everything_or_nothing_but_robots_txt() -> None:
    assert RobotsRules.allowing_all().allows(f"{SITE}/any")
    assert not RobotsRules.disallowing_all().allows(f"{SITE}/any")
    assert RobotsRules.disallowing_all().allows(f"{SITE}/robots.txt")
    assert RobotsRules.parse(b"User-agent: *\nDisallow: /", "pernix").allows(f"{SITE}/robots.txt")
    assert RobotsRules.disallowing_all().crawl_delay is None


def test_only_the_path_robots_txt_is_robots_txt() -> None:
    assert is_robots_txt(f"{SITE}/robots.txt")
    assert not is_robots_txt(f"{SITE}/robots.txt/more")
    assert not is_robots_txt(f"{SITE}/a/robots.txt")
