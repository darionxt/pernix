from __future__ import annotations

import datetime as dt
from typing import Any

import msgspec
import pytest

from pernix import Request, Response, Spider
from pernix.errors import SerializationError
from pernix.request_codec import REQUEST_SCHEMA_VERSION, decode_request, encode_request

URL = "https://example.com/item?id=1"


class Shop(Spider):
    async def parse_item(self, response: Response) -> None:
        pass

    async def failed(self, error: Exception) -> None:
        pass


class Renamed(Spider):
    async def _implementation(self, response: Response) -> None:
        pass

    handle = _implementation
    del _implementation


def _full_request(shop: Shop) -> Request:
    return Request(
        URL,
        method="POST",
        headers=[("X-Trace", "1"), ("x-trace", "2"), ("Accept", "*/*")],
        body=bytes(range(256)),
        callback=shop.parse_item,
        errback=shop.failed,
        kwargs={"category": "books", "page": 3},
        meta={"depth": 2, "path": ["a", "b"], "nested": {"ok": True, "ratio": 0.5, "none": None}},
        priority=7,
        dont_filter=True,
        fingerprint="item-1",
        cookies={"session": "abc"},
        session="account-1",
        render=True,
        fetcher="browser",
        timeout=12.5,
        proxy="http://user:pass@proxy.example:8080",
        impersonate="chrome146",
        ext={"retry": {"attempt": 2}},
    )


def test_every_field_round_trips() -> None:
    shop = Shop()
    request = _full_request(shop)

    restored = decode_request(encode_request(request, shop), shop)

    assert restored == request
    assert restored.headers.items() == [("X-Trace", "1"), ("x-trace", "2"), ("Accept", "*/*")]
    assert restored.callback == shop.parse_item


def test_minimal_requests_round_trip_with_defaults() -> None:
    shop = Shop()
    request = Request(URL)

    assert decode_request(encode_request(request, shop), shop) == request


def test_tuples_come_back_as_lists() -> None:
    shop = Shop()
    request = Request(URL, meta={"pair": (1, 2)})

    assert decode_request(encode_request(request, shop), shop).meta == {"pair": [1, 2]}


def test_missing_callbacks_are_an_error_not_a_fallback() -> None:
    shop = Shop()
    data = encode_request(Request(URL, callback=shop.parse_item), shop)

    class Changed(Spider):
        async def parse(self, response: Response) -> None:
            pass

    with pytest.raises(SerializationError, match=r"callback 'parse_item' is not a method of Changed; was it renamed"):
        decode_request(data, Changed())


def test_callbacks_must_belong_to_the_spider_being_persisted() -> None:
    other = Shop()

    with pytest.raises(SerializationError, match="is not a method of the Spider Shop"):
        encode_request(Request(URL, callback=other.parse_item), Shop())


def test_callbacks_must_be_reachable_by_their_name() -> None:
    spider = Renamed()

    with pytest.raises(SerializationError, match=r"cannot be found again as Renamed\._implementation"):
        encode_request(Request(URL, callback=spider.handle), spider)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("meta", {"when": dt.date(2026, 1, 1)}, r"meta\['when'\] holds a date, which is not JSON-compatible"),
        ("meta", {"tags": {"a", "b"}}, r"meta\['tags'\] holds a set"),
        ("kwargs", {"rows": [{"id": object()}]}, r"kwargs\['rows'\]\[0\]\['id'\] holds a object"),
        ("meta", {"lookup": {1: "a"}}, r"meta\['lookup'\] has the key 1; keys must be strings"),
        ("ext", {"retry": {"since": dt.datetime(2026, 1, 1, tzinfo=dt.UTC)}}, r"ext\['retry'\]\['since'\]"),
    ],
)
def test_user_data_must_be_json_compatible(field: str, value: Any, message: str) -> None:
    shop = Shop()

    with pytest.raises(SerializationError, match=message):
        encode_request(Request(URL, **{field: value}), shop)


def test_newer_incompatible_formats_are_rejected() -> None:
    data = msgspec.msgpack.encode({"schema_version": REQUEST_SCHEMA_VERSION + 1, "url": URL})

    with pytest.raises(SerializationError, match="upgrade Pernix to resume this Run"):
        decode_request(data, Shop())


def test_unknown_fields_from_newer_additive_changes_are_ignored() -> None:
    data = msgspec.msgpack.encode({"schema_version": REQUEST_SCHEMA_VERSION, "url": URL, "added_later": [1, 2]})

    assert decode_request(data, Shop()) == Request(URL)


def test_garbage_is_not_a_request() -> None:
    with pytest.raises(SerializationError, match="not a valid persisted Request"):
        decode_request(b"\x00not msgpack", Shop())
