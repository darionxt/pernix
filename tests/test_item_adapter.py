from __future__ import annotations

import dataclasses
import datetime as dt
import subprocess
import sys
from collections import OrderedDict
from decimal import Decimal
from types import MappingProxyType
from typing import Any

import attrs
import msgspec
import pydantic
import pytest

from pernix import Request, Response
from pernix.errors import ComponentError
from pernix.items import is_item, item_matcher, item_to_dict

WHEN = dt.datetime(2026, 10, 13, 8, 0, tzinfo=dt.UTC)


@dataclasses.dataclass
class Author:
    name: str


@dataclasses.dataclass
class BookData:
    title: str
    price: Decimal
    authors: list[Author]
    tags: tuple[str, ...] = ()


class BookStruct(msgspec.Struct, rename="camel"):
    title: str
    page_count: int
    author: Author


class BookModel(pydantic.BaseModel):
    title: str = pydantic.Field(alias="Title")
    published: dt.datetime
    author: Author


class TaggedModel(pydantic.BaseModel):
    tags: set[str]
    shelves: frozenset[int]


@attrs.define
class BookAttrs:
    title: str
    author: Author
    extra: dict[str, Any] = attrs.field(factory=dict[str, Any])


@pytest.mark.parametrize(
    ("item", "expected"),
    [
        ({"title": "Dune", "tags": {"sf"}}, {"title": "Dune", "tags": ["sf"]}),
        (MappingProxyType({"title": "Dune"}), {"title": "Dune"}),
        (OrderedDict(title="Dune"), {"title": "Dune"}),
        (
            BookData("Dune", Decimal("9.99"), [Author("Herbert")], ("sf",)),
            {"title": "Dune", "price": Decimal("9.99"), "authors": [{"name": "Herbert"}], "tags": ["sf"]},
        ),
        (
            BookStruct("Dune", 412, Author("Herbert")),
            {"title": "Dune", "pageCount": 412, "author": {"name": "Herbert"}},
        ),
        (
            BookModel(Title="Dune", published=WHEN, author=Author("Herbert")),
            {"title": "Dune", "published": WHEN, "author": {"name": "Herbert"}},
        ),
        (TaggedModel(tags={"sf"}, shelves=frozenset({2})), {"tags": ["sf"], "shelves": [2]}),
        (
            BookAttrs("Dune", Author("Herbert"), {"nested": BookStruct("x", 1, Author("y"))}),
            {
                "title": "Dune",
                "author": {"name": "Herbert"},
                "extra": {"nested": {"title": "x", "pageCount": 1, "author": {"name": "y"}}},
            },
        ),
    ],
    ids=["dict", "mapping-proxy", "ordered-dict", "dataclass", "msgspec", "pydantic", "pydantic-sets", "attrs"],
)
def test_every_item_type_becomes_a_plain_dict(item: object, expected: dict[str, Any]) -> None:
    assert is_item(item)

    converted = item_to_dict(item)

    assert converted == expected
    assert type(converted) is dict


def test_conversion_copies_the_item() -> None:
    item = {"authors": [{"name": "Herbert"}]}

    converted = item_to_dict(item)
    converted["authors"][0]["name"] = "changed"

    assert item == {"authors": [{"name": "Herbert"}]}


@pytest.mark.parametrize(
    "value",
    [
        None,
        42,
        "text",
        ["a", "list"],
        BookData,  # a dataclass type, not an instance
        Request("https://example.com/"),
        Response(url="https://example.com/", status=200, headers=[], body=b"", request=Request("https://example.com/")),
        object(),
    ],
)
def test_other_outputs_are_not_items(value: object) -> None:
    assert not is_item(value)
    with pytest.raises(TypeError, match="is not an Item"):
        item_to_dict(value)


def test_optional_libraries_are_not_imported() -> None:
    script = (
        "import sys, dataclasses, pernix.items as items\n"
        "assert not items.is_item(object()) and items.is_item({})\n"
        "assert 'pydantic' not in sys.modules and 'attr' not in sys.modules, sorted(sys.modules)\n"
    )

    finished = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=False)  # noqa: S603

    assert finished.returncode == 0, finished.stderr


@pytest.mark.parametrize(
    ("accepts", "item", "taken"),
    [
        (None, {"a": 1}, True),
        ((BookData,), BookData("Dune", Decimal(1), []), True),
        ((BookData,), {"title": "Dune"}, False),
        ([BookStruct, "builtins.dict"], {"title": "Dune"}, True),
        (lambda item: "isbn" in item, {"isbn": "1"}, True),
        (lambda item: "isbn" in item, {"title": "Dune"}, False),
    ],
)
def test_item_matchers_take_types_import_paths_or_functions(accepts: Any, item: object, taken: bool) -> None:
    assert item_matcher(accepts)(item) is taken


@pytest.mark.parametrize("accepts", [BookData, (BookData, 42), "builtins.dict"])
def test_item_matchers_reject_other_values(accepts: Any) -> None:
    with pytest.raises(TypeError, match="accepts must"):
        item_matcher(accepts)


def test_item_matcher_import_paths_must_load() -> None:
    with pytest.raises(ComponentError, match="no attribute"):
        item_matcher(["pernix.NoSuchItem"])
