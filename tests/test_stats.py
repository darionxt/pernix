from __future__ import annotations

import pytest

from pernix.stats import Stats


def test_counters_values_and_maxima() -> None:
    stats = Stats()

    stats.inc("books.pages")
    stats.inc("books.pages", 2)
    stats.inc("books.seconds", 0.5)
    stats.set("books.last", "page-3")
    stats.max("books.depth", 2)
    stats.max("books.depth", 1)

    assert stats.get("books.pages") == 3
    assert stats.get("books.missing") is None
    assert stats.get("books.missing", 0) == 0
    assert stats.snapshot() == {"books.depth": 2, "books.last": "page-3", "books.pages": 3, "books.seconds": 0.5}
    assert list(stats.snapshot()) == sorted(stats.snapshot())


def test_snapshots_are_copies() -> None:
    stats = Stats()
    snapshot = stats.snapshot()

    stats.inc("books.pages")

    assert snapshot == {}


@pytest.mark.parametrize("value", [True, None, [1], {"a": 1}, b"bytes"])
def test_values_are_ints_floats_or_strings(value: object) -> None:
    with pytest.raises(TypeError, match="must be an int, a float, or a string"):
        Stats().set("books.value", value)  # pyright: ignore[reportArgumentType]


def test_string_stats_cannot_be_counted_or_compared() -> None:
    stats = Stats()
    stats.set("books.last", "page-3")

    with pytest.raises(TypeError, match="cannot be incremented"):
        stats.inc("books.last")
    with pytest.raises(TypeError, match="cannot be compared"):
        stats.max("books.last", 1)
