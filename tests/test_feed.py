from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix.errors import FeedError, SettingsError
from pernix.feeds import Feed, FeedWriting, render_path
from pernix.settings import FeedOptions, FeedSettings, ItemSettings, load_settings

if TYPE_CHECKING:
    from pathlib import Path


@dataclasses.dataclass
class Book:
    title: str
    isbn: str


def test_a_feed_appears_only_once_closed(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "out" / "books.json")

    feed.open()
    feed.write({"title": "Dune"})
    feed.write(Book("Emma", "2"))

    assert not (tmp_path / "out" / "books.json").exists()
    [partial] = (tmp_path / "out").iterdir()
    assert partial.name.startswith(".books.json.")
    assert partial.suffix == ".part"

    feed.close()
    feed.close()  # closing again does nothing

    assert [path.name for path in (tmp_path / "out").iterdir()] == ["books.json"]
    assert orjson.loads((tmp_path / "out" / "books.json").read_bytes()) == [
        {"title": "Dune"},
        {"title": "Emma", "isbn": "2"},
    ]


def test_a_feed_replaces_an_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "books.jsonl"
    target.write_text("old\n")
    feed = Feed(target)

    feed.open()
    feed.write({"n": 1})
    assert target.read_text() == "old\n"
    feed.close()

    assert target.read_text() == '{"n":1}\n'


def test_fields_select_and_order_the_written_fields(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "books.jsonl", FeedOptions(fields=("isbn", "title", "missing")))

    feed.open()
    feed.write(Book("Dune", "1"))
    feed.close()

    assert (tmp_path / "books.jsonl").read_text() == '{"isbn":"1","title":"Dune"}\n'


def test_feeds_take_the_items_they_accept(tmp_path: Path) -> None:
    books = Feed(tmp_path / "books.csv", FeedOptions(accepts=("test_feed.Book",)))
    marked = Feed(tmp_path / "marked.csv", FeedOptions(accepts=lambda item: item.get("kind") == "author"))

    assert books.wants(Book("Dune", "1"))
    assert not books.wants({"title": "Dune"})
    assert marked.wants({"kind": "author"})
    assert not marked.wants({"kind": "book"})


def test_the_format_and_the_encoding_are_honored(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "books.txt", FeedOptions(format="csv", encoding="utf-8-sig"))

    feed.open()
    feed.write({"title": "Café"})
    feed.close()

    assert (tmp_path / "books.txt").read_bytes() == "﻿title\r\nCafé\r\n".encode()


def test_an_item_that_cannot_be_written_raises_a_feed_error(tmp_path: Path) -> None:
    feed = Feed(tmp_path / "items.jsonl")
    feed.open()

    with pytest.raises(FeedError, match=r"cannot write an Item to .*items\.jsonl: .*bytes") as raised:
        feed.write({"raw": b"\x00"})
    with pytest.raises(FeedError, match="is not an Item"):
        feed.write("text")
    feed.close()

    assert isinstance(raised.value.__cause__, TypeError)


def test_a_feed_written_to_while_closed_fails(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="is not open"):
        Feed(tmp_path / "books.jsonl").write({"n": 1})


def test_the_format_must_be_known(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="cannot infer the feed format"):
        Feed(tmp_path / "books.txt")


def test_paths_fill_in_the_spider_the_run_and_the_time() -> None:
    started = dt.datetime(2026, 10, 13, 16, 4, 5, tzinfo=dt.timezone(dt.timedelta(hours=8)))

    path = render_path("out/{name}/{run_id}-{time}.jsonl", name="books", run_id="r1", started=started)

    assert path.as_posix() == "out/books/r1-20261013T080405Z.jsonl"


@pytest.mark.parametrize(
    ("settings", "problem"),
    [
        ({"feeds": {"out/{who}.jsonl": {}}}, r"feeds\['out/\{who\}.jsonl'\]: unknown placeholders \['who'\]"),
        ({"feeds": {"out/{name.jsonl": {}}}, r"invalid path template"),
        ({"feeds": {"books.txt": {}}}, r"cannot infer the format from the extension"),
        ({"feeds": {"books.csv": {"encoding": "klingon"}}}, r"unknown encoding 'klingon'"),
        ({"feeds": {"books.csv": {"colums": ["a"]}}}, r"unknown field `colums`"),
        ({"items.failed_output": "failed-{when}.jsonl"}, r"items\.failed_output: unknown placeholders"),
    ],
)
def test_feed_settings_are_checked_at_startup(settings: dict[str, object], problem: str) -> None:
    with pytest.raises(SettingsError, match=problem):
        load_settings(overrides=settings)


def test_feed_settings_accept_tables_and_formats() -> None:
    settings = load_settings(
        overrides={"feeds": {"books.txt": {"format": "json", "fields": ["title"]}}, "items.failed_output": "f.jsonl"}
    )

    assert settings.section(FeedSettings).feeds == {"books.txt": FeedOptions(format="json", fields=("title",))}
    assert settings.section(ItemSettings).failed_output == "f.jsonl"


# Written in place, as a resumable Run writes its Feeds


def _written(path: Path, *records: dict[str, object], writing: FeedWriting, **options: Any) -> str:
    feed = Feed(path, FeedOptions(**options), writing=writing)
    feed.open()
    for record in records:
        feed.write(record)
    feed.close()
    return path.read_bytes().decode(str(options.get("encoding", "utf-8")))  # line endings as written


def test_a_feed_written_in_place_replaces_the_file_and_flushes_each_item(tmp_path: Path) -> None:
    target = tmp_path / "out" / "books.jsonl"
    target.parent.mkdir()
    target.write_text("old\n")
    feed = Feed(target, writing="in_place")

    feed.open()
    assert target.read_text() == ""
    feed.write({"n": 1})
    assert target.read_text() == '{"n":1}\n'  # before the Feed is closed
    feed.close()

    assert [path.name for path in target.parent.iterdir()] == ["books.jsonl"]


@pytest.mark.parametrize(
    ("name", "left", "kept"),
    [
        ("books.jsonl", '{"n":1}\n{"n":2}\n{"n"', '{"n":1}\n{"n":2}\n'),
        ("books.json", '[\n{"n":1},\n{"n":2},\n{"n"', '[\n{"n":1},\n{"n":2}'),
        ("books.csv", 'n,text\r\n1,"two\r\nlines"\r\n2,"cut\r\nsh', 'n,text\r\n1,"two\r\nlines"\r\n'),
    ],
    ids=["jsonl", "json", "csv"],
)
def test_a_continued_feed_cuts_a_partial_last_record_off_and_goes_on(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, name: str, left: str, kept: str
) -> None:
    target = tmp_path / name
    target.write_bytes(left.encode())
    feed = Feed(target, writing="continued")

    with caplog.at_level("WARNING", logger="pernix.feeds.feed"):
        feed.open()

    assert target.read_bytes() == kept.encode()
    assert f"Cut {len(left) - len(kept)} bytes of a partial record off the end of the feed {target}" in caplog.text
    feed.write({"n": 3, "text": "three"} if name.endswith(".csv") else {"n": 3})
    feed.close()
    match name.rsplit(".", 1)[1]:
        case "jsonl":
            assert [orjson.loads(line) for line in target.read_text().splitlines()] == [{"n": n} for n in (1, 2, 3)]
        case "json":
            assert orjson.loads(target.read_bytes()) == [{"n": n} for n in (1, 2, 3)]
        case _:
            assert target.read_bytes().decode() == 'n,text\r\n1,"two\r\nlines"\r\n3,three\r\n'  # one header


def test_a_json_array_ended_before_is_continued_and_ended_again(tmp_path: Path) -> None:
    target = tmp_path / "books.json"
    _written(target, {"n": 1}, writing="in_place")

    text = _written(target, {"n": 2}, writing="continued")

    assert text == '[\n{"n":1},\n{"n":2}\n]\n'
    assert _written(tmp_path / "empty.json", writing="in_place") == "[]\n"
    assert orjson.loads(_written(tmp_path / "empty.json", {"n": 1}, writing="continued")) == [{"n": 1}]


def test_a_continued_csv_takes_its_columns_from_its_header(tmp_path: Path) -> None:
    target = tmp_path / "books.csv"
    _written(target, {"title": "Dune", "year": 1965}, writing="in_place")

    text = _written(target, {"year": 1815, "title": "Emma"}, writing="continued")

    assert text == "title,year\r\nDune,1965\r\nEmma,1815\r\n"


def test_a_csv_whose_header_is_not_the_fields_cannot_be_continued(tmp_path: Path) -> None:
    target = tmp_path / "books.csv"
    _written(target, {"title": "Dune", "year": 1965}, writing="in_place")
    feed = Feed(target, FeedOptions(fields=("year", "title")), writing="continued")

    with pytest.raises(FeedError, match=r"cannot continue the feed .+: its columns are \['title', 'year'\], not"):
        feed.open()

    assert target.read_bytes() == b"title,year\r\nDune,1965\r\n"


def test_a_continued_feed_whose_file_is_missing_or_empty_begins_it(tmp_path: Path) -> None:
    (tmp_path / "empty.csv").write_text("")

    assert _written(tmp_path / "missing.json", {"n": 1}, writing="continued") == '[\n{"n":1}\n]\n'
    assert _written(tmp_path / "empty.csv", {"n": 1}, writing="continued", fields=("n",)) == "n\r\n1\r\n"


def test_writing_in_place_needs_an_encoding_where_ascii_characters_are_single_bytes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"cannot be written in place, as a resumable Run writes it, in utf-16"):
        Feed(tmp_path / "books.jsonl", FeedOptions(encoding="utf-16"), writing="in_place")

    _written(tmp_path / "books.jsonl", {"title": "Café"}, writing="in_place", encoding="latin-1")
    assert (tmp_path / "books.jsonl").read_bytes() == '{"title":"Café"}\n'.encode("latin-1")
