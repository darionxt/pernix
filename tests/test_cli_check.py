from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    WriteSpider = Callable[[str, str], Path]

BOOKS = """
    from pernix import Spider


    class Books(Spider):
        name = "books"
        settings = {"feeds": {"out/{name}.jsonl": {}}}

        def __init__(self, limit: int = 5) -> None:
            self.limit = limit
"""


@pytest.fixture(autouse=True)
def _in_empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_the_defaults_are_valid(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "OK",
        "Project file: none",
        "Fetchers: http (HTTPFetcher)",
        "Stats Sinks: none",
        "Extensions: OffsiteFilter",
        "Download Middleware: none",
        "Spider Middleware: none",
        "Pipelines: none",
        "Failure Pipelines: FailedItemsFile",
        "Feeds: none",
    ]


def test_a_spider_its_settings_and_its_arguments_are_checked(
    write_spider: WriteSpider, capsys: pytest.CaptureFixture[str]
) -> None:
    path = write_spider("check_books", BOOKS)

    assert main(["check", str(path), "-a", "limit=2"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert "Spider: Books (name 'books')" in out
    assert "Feeds: out/books.jsonl" in out

    assert main(["check", str(path), "-a", "limit=all"]) == 2
    assert "limit: Expected `int`, got `str`" in capsys.readouterr().err


def test_a_misspelled_setting_is_reported_with_a_suggestion(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pernix.toml").write_text("concurency = 4\n")

    assert main(["check", "-s", "domain.dalay=1"]) == 2

    err = capsys.readouterr().err
    assert "unknown setting 'concurency' from pernix.toml" in err
    assert "did you mean 'concurrency'?" in err
    assert "unknown setting 'domain.dalay' from -s domain.dalay; did you mean 'domain.delay'?" in err


def test_components_that_cannot_be_built_are_reported(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "-s", "retry.policy=builtins.object"]) == 2
    assert "retry.policy must have decide(request, outcome, attempt)" in capsys.readouterr().err


def test_the_project_file_in_use_is_named(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pernix.toml").write_text('[pipelines]\n"pernix.items.FailedItemsFile" = 10\n')

    assert main(["check"]) == 0

    assert f"Project file: pernix.toml ({tmp_path.resolve() / 'pernix.toml'})" in capsys.readouterr().out
