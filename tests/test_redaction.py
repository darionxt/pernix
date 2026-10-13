from __future__ import annotations

from types import MappingProxyType

import pytest

from pernix.logs import Redactor, revealed_secrets
from pernix.settings import Secret


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        ("proxy http://alice:s3cret@proxy.example:8080", "proxy http://***@proxy.example:8080"),
        ("socks5h://user:p%40ss@10.0.0.1:1080/", "socks5h://***@10.0.0.1:1080/"),
        ("https://token@api.example/v1", "https://***@api.example/v1"),
        ("<Request GET https://u:p@shop.example/a?b=1>", "<Request GET https://***@shop.example/a?b=1>"),
        ("{'proxy': 'http://alice:s3cret@proxy:8080'}", "{'proxy': 'http://***@proxy:8080'}"),
    ],
)
def test_url_credentials_are_masked(text: str, redacted: str) -> None:
    assert Redactor().redact(text) == redacted


@pytest.mark.parametrize(
    "text",
    [
        "https://example.com/path?email=a@b.example",
        "mail me at someone@example.com",
        "https://example.com:8443/a",
    ],
)
def test_text_without_credentials_is_kept(text: str) -> None:
    assert Redactor().redact(text) == text


@pytest.mark.parametrize(
    ("text", "redacted"),
    [
        (
            "https://api.example/v1?q=books&access_token=abc123&page=2",
            "https://api.example/v1?q=books&access_token=***&page=2",
        ),
        ("https://s3.example/o?X-Amz-Signature=deadbeef#top", "https://s3.example/o?X-Amz-Signature=***#top"),
        ("body: user=alice&password=hunter2", "body: user=alice&password=***"),
        ("Cookie: sessionid=abc; theme=dark", "Cookie: ***"),
        ("set-cookie: sid=1; Path=/\nnext line", "set-cookie: ***\nnext line"),
        ("Authorization: Bearer eyJhbGciOi", "Authorization: ***"),
        ("Headers([('Cookie', 'sid=1'), ('Accept', '*/*')])", "Headers([('Cookie', '***'), ('Accept', '*/*')])"),
        ("{'Proxy-Authorization': 'Basic YWxp'}", "{'Proxy-Authorization': '***'}"),
        ('{"password": "hunter2", "user": "alice"}', '{"password": "***", "user": "alice"}'),
        ('{"api_key":12345}', '{"api_key":***}'),
        ("?monkey=1&key=2", "?monkey=1&key=***"),
    ],
)
def test_values_of_sensitive_names_are_masked(text: str, redacted: str) -> None:
    assert Redactor().redact(text) == redacted


def test_prose_mentioning_sensitive_names_is_kept() -> None:
    text = "Ignoring a Set-Cookie header without a name=value pair; the token expired"

    assert Redactor().redact(text) == text


def test_extra_names_are_masked_in_every_form() -> None:
    redactor = Redactor(names=["X-Shop-Ticket", " "])

    assert redactor.redact("?x-shop-ticket=42") == "?x-shop-ticket=***"
    assert redactor.redact("X-Shop-Ticket: 42") == "X-Shop-Ticket: ***"
    assert redactor.redact("{'x-shop-ticket': '42'}") == "{'x-shop-ticket': '***'}"


def test_secret_values_are_masked_wherever_they_occur() -> None:
    redactor = Redactor(secrets=["sk-live-123", "sk-live-123456", ""])

    assert redactor.redact("key sk-live-123456 and sk-live-123.") == "key *** and ***."


def test_secrets_are_found_inside_the_settings_values() -> None:
    values = [
        Secret("a"),
        "plain",
        ("x", Secret("b")),
        MappingProxyType({"accounts": [Secret("c")], Secret("d"): 1}),
        frozenset({Secret("e")}),
    ]

    assert revealed_secrets(values) == {"a", "b", "c", "d", "e"}


def test_strings_inside_values_are_masked_in_copies() -> None:
    redactor = Redactor(secrets=["sk-live-123"])
    stats = {
        "proxy.last": "http://alice:s3cret@proxy:8080",
        "sk-live-123": 1,
        "nested": [{"key": ("sk-live-123", 2)}, {"sk-live-123"}, frozenset({"plain"})],
        "count": 3,
    }

    redacted = redactor.redact_value(stats)

    assert redacted == {
        "proxy.last": "http://***@proxy:8080",
        "***": 1,
        "nested": [{"key": ("***", 2)}, {"***"}, frozenset({"plain"})],
        "count": 3,
    }
    assert stats["proxy.last"] == "http://alice:s3cret@proxy:8080"  # the original is unchanged
    assert redactor.redact_value(42) == 42


def test_containers_inside_themselves_or_too_deep_are_elided() -> None:
    cyclic: dict[str, object] = {"url": "https://api.example/?token=abc"}
    cyclic["self"] = cyclic
    looped: list[object] = ["sk"]
    looped.append(looped)
    deep: list[object] = ["leaf"]
    for _ in range(500):
        deep = [deep]

    assert Redactor().redact_value(cyclic) == {"url": "https://api.example/?token=***", "self": "..."}
    assert Redactor().redact_value(looped) == ["sk", "..."]
    masked = {"url": "https://api.example/?token=***", "self": "..."}
    assert Redactor().redact_value({"a": cyclic, "b": cyclic}) == {"a": masked, "b": masked}  # shared, not cyclic
    assert "leaf" not in repr(Redactor().redact_value(deep))
