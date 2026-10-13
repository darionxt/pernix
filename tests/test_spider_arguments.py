from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from typing import Any, Literal

import pytest

from pernix import Spider
from pernix.errors import SpiderArgumentError
from pernix.settings import Secret
from pernix.spider_arguments import create_spider, spider_arguments


@dataclasses.dataclass
class Window:
    start: int
    end: int


class Search(Spider):
    def __init__(
        self,
        keyword: str,
        max_pages: int = 10,
        *,
        exact: bool = False,
        ratio: float = 1.0,
        tags: list[str] | None = None,
        window: Window | None = None,
        token: Secret | None = None,
        out: Path | None = None,
        sort: Literal["new", "top"] = "new",
        raw=None,  # unannotated on purpose
    ) -> None:
        self.keyword = keyword
        self.max_pages = max_pages
        self.exact = exact
        self.ratio = ratio
        self.tags = tags
        self.window = window
        self.token = token
        self.out = out
        self.sort = sort
        self.raw = raw


def test_strings_are_converted_to_the_annotated_types() -> None:
    spider = create_spider(
        Search,
        {
            "keyword": "dune",
            "max_pages": "3",
            "exact": "true",
            "ratio": "0.5",
            "tags": '["a", "b"]',
            "window": '{"start": 1, "end": 2}',
            "token": "s3cr3t",
            "out": "out/books.jsonl",
            "sort": "top",
            "raw": "as is",
        },
    )

    assert isinstance(spider, Search)
    assert (spider.keyword, spider.max_pages, spider.exact, spider.ratio) == ("dune", 3, True, 0.5)
    assert spider.tags == ["a", "b"]
    assert spider.window == Window(1, 2)
    assert spider.token == Secret("s3cr3t")
    assert spider.out == Path("out/books.jsonl")
    assert (spider.sort, spider.raw) == ("top", "as is")


def test_other_values_must_already_have_the_annotated_types() -> None:
    converted = spider_arguments(Search, {"keyword": "dune", "max_pages": 3, "window": Window(1, 2), "raw": [1]})

    assert converted == {"keyword": "dune", "max_pages": 3, "window": Window(1, 2), "raw": [1]}
    with pytest.raises(SpiderArgumentError, match=r"keyword: Expected `str`, got `int`"):
        spider_arguments(Search, {"keyword": 3})


def test_every_problem_is_reported_at_once() -> None:
    with pytest.raises(SpiderArgumentError) as raised:
        spider_arguments(Search, {"max_pages": "many", "exat": "true", "tags": "a,b", "sort": "old"})

    assert raised.value.problems == [
        "max_pages: Expected `int`, got `str`",
        "unknown argument 'exat'; did you mean 'exact'?",
        "tags: invalid JSON: unexpected character, expected a JSON value: line 1 column 1 (char 0)",
        "sort: Invalid enum value 'old'",
        "missing argument 'keyword'",
    ]
    assert str(raised.value).startswith("Invalid Spider arguments:\n  - max_pages:")


def test_a_spider_without_parameters_takes_no_arguments() -> None:
    class Plain(Spider):
        pass

    assert isinstance(create_spider(Plain, {}), Plain)
    with pytest.raises(SpiderArgumentError, match=r"unknown argument 'keyword': .*Plain takes no arguments"):
        create_spider(Plain, {"keyword": "dune"})


def test_keyword_catch_alls_take_any_argument_as_is() -> None:
    class Open(Spider):
        def __init__(self, page: int = 1, **options: Any) -> None:
            self.page = page
            self.options = options

    spider = create_spider(Open, {"page": "2", "lang": "en"})

    assert isinstance(spider, Open)
    assert (spider.page, spider.options) == (2, {"lang": "en"})


def test_required_positional_only_parameters_are_rejected() -> None:
    namespace: dict[str, Any] = {"Spider": Spider}
    source = "class Positional(Spider):\n    def __init__(self, keyword: str, /) -> None:\n        pass\n"

    with pytest.raises(TypeError, match=r"Positional\.__init__ requires 'keyword' as positional-only"):
        exec(source, namespace)  # noqa: S102  # a class statement made up above


def test_a_spider_may_reject_its_arguments_with_value_error() -> None:
    class Bounded(Spider):
        def __init__(self, max_pages: int = 10) -> None:
            if max_pages < 1:
                raise ValueError(f"max_pages must be positive, got {max_pages}")
            self.max_pages = max_pages

    with pytest.raises(SpiderArgumentError, match="Bounded: max_pages must be positive, got 0") as raised:
        create_spider(Bounded, {"max_pages": "0"})

    assert isinstance(raised.value.__cause__, ValueError)


def test_other_errors_of_the_spider_are_not_argument_errors() -> None:
    class Broken(Spider):
        def __init__(self) -> None:
            raise KeyError("bug")

    with pytest.raises(KeyError):
        create_spider(Broken, {})


@pytest.mark.parametrize("name", ["settings", "output"])
def test_arguments_cannot_use_the_names_of_the_run_options(name: str) -> None:
    namespace: dict[str, Any] = {"Spider": Spider}
    source = f"class Clash(Spider):\n    def __init__(self, {name}: str) -> None:\n        pass\n"

    with pytest.raises(
        TypeError, match=f"Clash.__init__ takes '{name}', which run\\(\\), crawl\\(\\), and stream\\(\\)"
    ):
        exec(source, namespace)  # noqa: S102  # a class statement made up above


@pytest.mark.skipif(sys.version_info < (3, 14), reason="annotations are evaluated lazily from Python 3.14 on")
def test_annotations_may_name_types_defined_later() -> None:
    namespace: dict[str, Any] = {"Spider": Spider}
    source = (
        "class Later(Spider):\n"
        "    def __init__(self, window: Window) -> None:\n"
        "        self.window = window\n"
        "class Window:\n"
        "    pass\n"
    )
    # Compiled without this module's `from __future__ import annotations`.
    exec(compile(source, "<spiders>", "exec", dont_inherit=True), namespace)  # noqa: S102

    window = namespace["Window"]()
    spider = create_spider(namespace["Later"], {"window": window})

    assert spider.window is window  # pyright: ignore[reportAttributeAccessIssue]
