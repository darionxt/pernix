from __future__ import annotations

import datetime as dt
from typing import Any

import msgspec
import pytest

from pernix import Request, Response, Spider
from pernix.browser import Click, Evaluate, PageMethod, Render, Screenshot, Scroll, Type, WaitFor
from pernix.errors import SerializationError
from pernix.request_codec import REQUEST_SCHEMA_VERSION, decode_request, encode_request, method_names

URL = "https://example.com/item?id=1"


class Shop(Spider):
    async def parse_item(self, response: Response) -> None:
        pass

    async def failed(self, error: Exception) -> None:
        pass

    async def log_in(self, page: object) -> None:
        pass

    async def close_banner(self, page: object) -> None:
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


def test_render_options_round_trip_with_their_actions() -> None:
    shop = Shop()
    render = Render(
        wait_for="ul.books",
        actions=[
            WaitFor("li.book", state="attached", timeout=5),
            Scroll(times=3, delay=0.25),
            Click("#accept", optional=True, timeout=2),
            Type("input[name=q]", "dune", delay=0.05),
            Evaluate("() => window.__DATA__", isolated=True),
            Screenshot(full_page=False),
        ],
        block=("image", "font"),
        capture=("**/api/list*", "**/api/item/*"),
        screenshot=True,
    )
    request = Request(URL, render=render, callback=shop.parse_item)

    restored = decode_request(encode_request(request, shop), shop)

    assert restored == request
    assert restored.render == render
    assert [type(action) for action in render.actions] == [WaitFor, Scroll, Click, Type, Evaluate, Screenshot]


def test_page_methods_round_trip_by_name() -> None:
    shop = Shop()
    render = Render(setup=shop.log_in, action=PageMethod(shop.close_banner, optional=True, timeout=5.0), keep_page=True)
    request = Request(URL, render=render, callback=shop.parse_item)

    data = encode_request(request, shop)
    restored = decode_request(data, Shop())  # the Spider of a later Run, as on resume

    assert msgspec.msgpack.decode(data)["render"] == {
        "setup": {"name": "log_in"},
        "action": {"name": "close_banner", "optional": True, "timeout": 5.0},
        "keep_page": True,
    }
    assert isinstance(restored.render, Render)
    assert restored.render.setup is not None
    assert restored.render.action is not None
    assert (restored.render.setup.method.__name__, restored.render.action.optional) == ("log_in", True)
    assert (restored.render.action.timeout, restored.render.keep_page) == (5.0, True)


def test_a_page_method_missing_on_resume_fails_at_once() -> None:
    shop = Shop()
    data = encode_request(Request(URL, render=Render(action=shop.close_banner)), shop)

    class Changed(Spider):
        async def parse(self, response: Response) -> None:
            pass

    with pytest.raises(SerializationError, match=r"Render action 'close_banner' is not a method of Changed"):
        decode_request(data, Changed())


def test_page_methods_must_belong_to_the_spider_being_persisted() -> None:
    other = Shop()

    with pytest.raises(SerializationError, match=r"Render setup .* is not a method of the Spider Shop"):
        encode_request(Request(URL, render=Render(setup=other.log_in)), Shop())


def test_a_persisted_page_method_that_is_not_async_is_refused() -> None:
    class Synchronous(Spider):
        def close_banner(self, page: object) -> None:
            pass

    shop = Shop()
    data = encode_request(Request(URL, render=Render(action=shop.close_banner)), shop)

    with pytest.raises(SerializationError, match=r"Render options are invalid: PageMethod: .* is not an async method"):
        decode_request(data, Synchronous())


@pytest.mark.parametrize("render", [True, False, Render(), Render(block=()), Render(block=("media",))])
def test_render_true_and_render_options_stay_apart(render: bool | Render) -> None:
    shop = Shop()

    assert decode_request(encode_request(Request(URL, render=render), shop), shop).render == render


def test_actions_are_persisted_tagged_by_kind() -> None:
    shop = Shop()
    request = Request(URL, render=Render(actions=[Click("a.more"), Scroll(times=2)]))

    record = msgspec.msgpack.decode(encode_request(request, shop))

    assert record["render"] == {
        "actions": [{"action": "click", "selector": "a.more"}, {"action": "scroll", "times": 2}],
    }


@pytest.mark.parametrize(
    ("render", "message"),
    [
        (
            {"block": ["document"]},
            "the persisted Request's Render options are invalid: Render\\(block=...\\): 'document'",
        ),
        ({"actions": [{"action": "click", "selector": ""}]}, "not a valid persisted Request: Click: selector"),
        ({"actions": [{"action": "hover", "selector": "a"}]}, "not a valid persisted Request"),
    ],
)
def test_invalid_persisted_render_options_are_refused(render: dict[str, Any], message: str) -> None:
    data = msgspec.msgpack.encode({"schema_version": REQUEST_SCHEMA_VERSION, "url": URL, "render": render})

    with pytest.raises(SerializationError, match=message):
        decode_request(data, Shop())


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


def test_nested_read_only_mappings_are_stored_as_plain_data() -> None:
    shop = Shop()
    parent = Request(URL, meta={"depth": 1, "trail": ["a"]})
    request = Request(URL, meta={"parent": parent.meta}, ext={"retry": {"history": parent.meta}})

    restored = decode_request(encode_request(request, shop), shop)

    assert restored.meta == {"parent": {"depth": 1, "trail": ["a"]}}
    assert restored.ext["retry"] == {"history": {"depth": 1, "trail": ["a"]}}


def test_integers_must_fit_in_64_bits() -> None:
    shop = Shop()
    limits = Request(URL, meta={"low": -(2**63), "high": 2**64 - 1})

    assert decode_request(encode_request(limits, shop), shop).meta == {"low": -(2**63), "high": 2**64 - 1}
    with pytest.raises(SerializationError, match=r"meta\['ids'\]\[1\] holds the integer 1208925819614629174706176"):
        encode_request(Request(URL, meta={"ids": [1, 2**80]}), shop)
    with pytest.raises(SerializationError, match=r"kwargs\['offset'\] holds the integer"):
        encode_request(Request(URL, kwargs={"offset": -(2**63) - 1}), shop)


def test_the_methods_a_request_is_stored_by_are_named() -> None:
    shop = Shop()
    render = Render(setup=shop.log_in, action=PageMethod(shop.close_banner))

    assert method_names(Request(URL)) == ()
    assert method_names(Request(URL, callback=shop.parse_item, errback=shop.failed, render=render)) == (
        "parse_item",
        "failed",
        "log_in",
        "close_banner",
    )
