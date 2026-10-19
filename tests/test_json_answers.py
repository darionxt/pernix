"""Reading the JSON answers of proxy APIs: their lists, entries, and expiries."""

from __future__ import annotations

import datetime as dt
import json

import msgspec
import pytest

from pernix.proxies import JsonAnswer, Proxy
from pernix.proxies.json_answers import read_json_answer

NOW = 1_800_000_000.0


def _read(answer: object, **mapping: str | None) -> tuple[list[Proxy], list[str]]:
    return read_json_answer(json.dumps(answer), JsonAnswer(**mapping), now=NOW)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("answer", "path"),
    [
        (["a.example:1", "b.example:2"], ""),
        ({"data": {"proxy_list": ["a.example:1", "b.example:2"]}}, "data.proxy_list"),
        ({"result": [{"items": ["a.example:1", "b.example:2"]}]}, "result.0.items"),
    ],
    ids=["the answer itself", "nested", "through a list"],
)
def test_the_list_is_found_by_its_path_and_string_entries_are_addresses(answer: object, path: str) -> None:
    assert _read(answer, list=path) == ([Proxy("a.example:1"), Proxy("b.example:2")], [])


def test_an_object_entry_is_read_through_its_address_or_its_parts() -> None:
    whole = {"data": [{"server": "a.example:1"}]}
    parts = {
        "data": [{"ip": "b.example", "port": 2, "auth": {"user": "u@1", "pass": "p:w/"}}, {"ip": "::1", "port": "3"}]
    }

    assert _read(whole, list="data", address="server") == ([Proxy("a.example:1")], [])
    assert _read(parts, list="data", host="ip", port="port", username="auth.user", password="auth.pass") == (
        [Proxy("http://u%401:p:w%2F@b.example:2"), Proxy("http://[::1]:3")],
        [],
    )
    assert _read(parts, list="data", host="ip", port="port", scheme="socks5")[0][1] == Proxy("socks5://[::1]:3")


@pytest.mark.parametrize(
    ("value", "expires_format", "timezone", "expires"),
    [
        (1_800_000_600, "timestamp", None, 1_800_000_600.0),
        ("1800000600", "timestamp", None, 1_800_000_600.0),
        (1_800_000_600_000, "timestamp_ms", None, 1_800_000_600.0),
        (856, "seconds", None, NOW + 856),
        ("2027-01-15 16:00:00", "datetime", "+08:00", dt.datetime(2027, 1, 15, 8, tzinfo=dt.UTC).timestamp()),
        ("2027-01-15T16:00:00", "datetime", "Asia/Shanghai", dt.datetime(2027, 1, 15, 8, tzinfo=dt.UTC).timestamp()),
        ("2027-01-15 08:00:00Z", "datetime", "+08:00", dt.datetime(2027, 1, 15, 8, tzinfo=dt.UTC).timestamp()),
    ],
    ids=["timestamp", "timestamp as text", "milliseconds", "seconds left", "offset", "time zone", "own offset"],
)
def test_each_entry_expires_as_its_answer_tells(
    value: object, expires_format: str, timezone: str | None, expires: float
) -> None:
    answer = [{"ip": "a.example", "port": 1, "deadline": value}]

    [proxy], problems = _read(
        answer, host="ip", port="port", expires="deadline", expires_format=expires_format, timezone=timezone
    )

    assert proxy.expires == pytest.approx(expires)
    assert problems == []


def test_an_entry_without_its_expiry_has_none_and_one_that_cannot_be_read_is_named() -> None:
    answer = [{"ip": "a.example", "port": 1}, {"ip": "b.example", "port": 2, "deadline": "soon"}]

    proxies, problems = _read(
        answer, host="ip", port="port", expires="deadline", expires_format="datetime", timezone="UTC"
    )

    assert [proxy.expires for proxy in proxies] == [None]
    assert problems == ["#2 (`deadline` is not a datetime)"]


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("ERROR(1): out of credit", "it is not JSON"),
        ('{"code": 1, "msg": "out of credit"}', "it has no `data.proxy_list`"),
        ('{"data": {"proxy_list": "none"}}', "`data.proxy_list` is not a list"),
    ],
    ids=["not JSON", "no list", "not a list"],
)
def test_an_answer_that_does_not_match_gives_no_proxy_and_says_why_without_quoting_it(text: str, problem: str) -> None:
    assert read_json_answer(text, JsonAnswer(list="data.proxy_list"), now=NOW) == ([], [problem])


def test_entries_that_cannot_be_read_are_named_by_position_and_field_never_by_value() -> None:
    answer = [
        {"ip": "a.example", "port": 1},
        {"ip": "secret.example"},
        {"port": 3},
        42,
        {"ip": "c.example", "port": "x"},
    ]

    proxies, problems = _read(answer, host="ip", port="port")

    assert proxies == [Proxy("http://a.example:1")]
    assert problems == [
        "#2 (no `port`)",
        "#3 (no `ip`)",
        "#4 (neither a string nor an object)",
        "#5 (`port` is not as expected)",
    ]
    assert "secret" not in str(problems)


@pytest.mark.parametrize(
    ("mapping", "problem"),
    [
        ({"host": "ip"}, "host and port: an entry's host needs its port"),
        ({"address": "server", "host": "ip", "port": "port"}, "address and host: an entry is read through one"),
        ({"scheme": "ftp"}, "scheme: one of"),
        ({"expires": "deadline", "expires_format": "datetime"}, "timezone: a datetime written without an offset"),
        ({"timezone": "Mars/Olympus"}, "install the tzdata package where the system has no time zone data"),
        ({"lists": "data"}, "unknown field `lists`"),
    ],
    ids=["host without port", "address and host", "scheme", "datetime without time zone", "unknown zone", "unknown"],
)
def test_a_mapping_that_cannot_be_read_is_refused(mapping: dict[str, str], problem: str) -> None:
    with pytest.raises((ValueError, msgspec.ValidationError), match=problem):
        msgspec.convert(mapping, JsonAnswer)
