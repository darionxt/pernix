from __future__ import annotations

import sys
from typing import TYPE_CHECKING

import pytest

from pernix.cli.options import UsageError
from pernix.cli.spider_loader import _split, load_spider  # pyright: ignore[reportPrivateUsage]

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    WriteSpider = Callable[[str, str], Path]

TWO_SPIDERS = """
    from pernix import Spider


    class Books(Spider):
        name = "books"


    class Authors(Spider):
        name = "writers"
"""


def test_a_module_with_one_spider_needs_no_name(write_spider: WriteSpider) -> None:
    path = write_spider("loader_one", "from pernix import Spider\n\nclass Books(Spider):\n    pass\n")

    spider = load_spider(str(path))

    assert (spider.__name__, spider.__module__) == ("Books", "loader_one")


@pytest.mark.parametrize("name", ["Authors", "writers"])
def test_a_spider_is_chosen_by_class_name_or_spider_name(write_spider: WriteSpider, name: str) -> None:
    path = write_spider("loader_two", TWO_SPIDERS)

    assert load_spider(f"{path}:{name}").__name__ == "Authors"


def test_several_spiders_need_a_name(write_spider: WriteSpider) -> None:
    path = write_spider("loader_several", TWO_SPIDERS)

    with pytest.raises(UsageError, match=r"defines several Spiders that match: Authors \(name 'writers'\), Books"):
        load_spider(str(path))
    with pytest.raises(UsageError, match=r"defines no Spider named 'Shops'; Spiders defined there: Authors"):
        load_spider(f"{path}:Shops")


def test_spiders_imported_from_elsewhere_do_not_count(write_spider: WriteSpider) -> None:
    write_spider("loader_base", "from pernix import Spider\n\nclass Base(Spider):\n    pass\n")
    path = write_spider("loader_child", "from loader_base import Base\n\nclass Child(Base):\n    pass\n")

    assert load_spider(str(path)).__name__ == "Child"  # its sibling module was importable


def test_a_module_without_spiders_is_reported(write_spider: WriteSpider) -> None:
    path = write_spider("loader_none", "VALUE = 1\n")

    with pytest.raises(UsageError, match=r"defines no Spider; Spiders defined there: none"):
        load_spider(str(path))


def test_spiders_load_from_import_paths(write_spider: WriteSpider, monkeypatch: pytest.MonkeyPatch) -> None:
    path = write_spider("loader_package_books", TWO_SPIDERS)
    monkeypatch.syspath_prepend(str(path.parent))

    assert load_spider("loader_package_books:books").__name__ == "Books"
    with pytest.raises(UsageError, match=r"cannot import 'loader_nowhere': No module named 'loader_nowhere'"):
        load_spider("loader_nowhere")


def test_files_that_fail_to_import_are_reported_with_their_traceback(write_spider: WriteSpider, tmp_path: Path) -> None:
    broken = write_spider("loader_broken", "from pernix import Spider\n\nclass Broken(Spider)\n")
    failing = write_spider("loader_failing", "from pernix import Spider\n\nraise RuntimeError('no config')\n")

    with pytest.raises(UsageError, match=r"importing .*loader_broken\.py failed:\n") as broken_error:
        load_spider(str(broken))
    with pytest.raises(UsageError, match=r"importing .*loader_failing\.py failed:\n") as failing_error:
        load_spider(str(failing))
    with pytest.raises(UsageError, match=r"no such file: .*missing\.py"):
        load_spider(str(tmp_path / "missing.py"))

    assert str(broken_error.value).endswith(
        "    class Broken(Spider)\n                        ^\nSyntaxError: expected ':'"
    )
    message = str(failing_error.value)
    assert 'loader_failing.py", line 3, in <module>' in message
    assert message.endswith("RuntimeError: no config")
    assert "spider_loader.py" not in message
    assert "loader_failing" not in sys.modules


def test_a_file_named_like_a_loaded_module_is_refused(write_spider: WriteSpider) -> None:
    path = write_spider("json", "from pernix import Spider\n\nclass Shadow(Spider):\n    pass\n")

    with pytest.raises(UsageError, match=r"the module name 'json' is taken; rename the file"):
        load_spider(str(path))


@pytest.mark.parametrize(
    ("target", "split"),
    [
        ("books.py", ("books.py", None)),
        ("books.py:Books", ("books.py", "Books")),
        ("myproject.spiders.books:books", ("myproject.spiders.books", "books")),
        ("C:\\spiders\\books.py", ("C:\\spiders\\books.py", None)),
        ("C:\\spiders\\books.py:Books", ("C:\\spiders\\books.py", "Books")),
    ],
)
def test_targets_split_off_a_name_but_keep_paths_whole(target: str, split: tuple[str, str | None]) -> None:
    assert _split(target) == split
