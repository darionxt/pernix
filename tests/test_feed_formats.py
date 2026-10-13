from __future__ import annotations

import datetime as dt
import io
import logging
from decimal import Decimal

import orjson
import pytest

from pernix.feeds import CsvFormat, JsonArrayFormat, JsonLinesFormat, dump_json, feed_format


def _write(layout: JsonLinesFormat | JsonArrayFormat | CsvFormat, records: list[dict[str, object]]) -> str:
    out = io.StringIO(newline="")
    layout.begin(out)
    for record in records:
        layout.write(out, record)
    layout.end(out)
    return out.getvalue()


RECORDS: list[dict[str, object]] = [{"title": "Dune", "price": Decimal("9.99")}, {"title": "Emma", "tags": ["x"]}]


def test_json_lines_hold_one_object_per_line() -> None:
    assert _write(JsonLinesFormat(), RECORDS) == ('{"title":"Dune","price":"9.99"}\n{"title":"Emma","tags":["x"]}\n')


@pytest.mark.parametrize(("records", "count"), [(RECORDS, 2), ([], 0)])
def test_json_arrays_are_valid_and_hold_one_record_per_line(records: list[dict[str, object]], count: int) -> None:
    text = _write(JsonArrayFormat(), records)

    assert len(orjson.loads(text)) == count
    if records:
        assert text == '[\n{"title":"Dune","price":"9.99"},\n{"title":"Emma","tags":["x"]}\n]\n'


def test_a_failing_record_leaves_a_json_array_well_formed() -> None:
    layout = JsonArrayFormat()
    out = io.StringIO()
    layout.begin(out)
    layout.write(out, {"n": 1})
    with pytest.raises(TypeError):
        layout.write(out, {"raw": b"\x00"})
    layout.end(out)

    assert orjson.loads(out.getvalue()) == [{"n": 1}]


def test_csv_columns_come_from_the_first_record(caplog: pytest.LogCaptureFixture) -> None:
    records: list[dict[str, object]] = [
        {"title": "Dune", "price": 9.5, "in_stock": True},
        {"title": "Emma", "price": None, "authors": ["Austen"], "extra": 1},
        {"title": "Kim", "extra": 2},
    ]

    text = _write(CsvFormat(), records)

    assert text.splitlines() == ["title,price,in_stock", "Dune,9.5,true", "Emma,,", "Kim,,"]
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        "Dropping the field 'authors', which is not a column of the CSV feed",
        "Dropping the field 'extra', which is not a column of the CSV feed",
    ]


def test_csv_fields_choose_the_columns_and_cells_are_converted() -> None:
    when = dt.datetime(2026, 10, 13, 8, 0, tzinfo=dt.UTC)
    record: dict[str, object] = {
        "title": 'Say "hi", ok',
        "meta": {"pages": 3},
        "when": when,
        "day": when.date(),
        "price": Decimal("1.50"),
    }

    text = _write(CsvFormat(["day", "when", "meta", "title", "price", "missing"]), [record])

    assert text.splitlines() == [
        "day,when,meta,title,price,missing",
        '2026-10-13,2026-10-13T08:00:00+00:00,"{""pages"":3}","Say ""hi"", ok",1.50,',
    ]


def test_a_csv_with_known_fields_has_a_header_even_without_records() -> None:
    assert _write(CsvFormat(["title"]), []).splitlines() == ["title"]
    assert _write(CsvFormat(), []) == ""


def test_json_keeps_decimal_precision_and_rejects_unknown_types() -> None:
    assert dump_json({"price": Decimal("0.10"), 1: "non-string key"}) == '{"price":"0.10","1":"non-string key"}'
    with pytest.raises(TypeError):
        dump_json({"raw": b"\x00"})


def test_formats_are_chosen_by_name() -> None:
    assert isinstance(feed_format("jsonl"), JsonLinesFormat)
    assert isinstance(feed_format("json"), JsonArrayFormat)
    assert isinstance(feed_format("csv", fields=["a"]), CsvFormat)
    with pytest.raises(ValueError, match="unknown feed format 'xml'"):
        feed_format("xml")
