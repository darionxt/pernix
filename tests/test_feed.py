from __future__ import annotations

import dataclasses
import datetime as dt
from typing import TYPE_CHECKING

import orjson
import pytest

from pernix.errors import FeedError, SettingsError
from pernix.feeds import Feed, render_path
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
