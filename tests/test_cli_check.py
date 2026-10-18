from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from pernix.addons import Addon
from pernix.cli import main

if TYPE_CHECKING:
    from collections.abc import Callable

    WriteSpider = Callable[[str, str], Path]

BOOKS = """
    from pernix import Spider


    class Books(Spider):
        name = "books"
        settings = {"feeds": {"out/{name}.jsonl": {}}}

        def __init__(self, limit: int = 5) -> None:
            self.limit = limit
"""


pytestmark = pytest.mark.usefixtures("without_patchright")  # the browser is checked in test_cli_check_browser.py


@pytest.fixture(autouse=True)
def _in_empty_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_the_defaults_are_valid(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == 0

    assert capsys.readouterr().out.splitlines() == [
        "OK",
        "Project file: none",
        "Fetchers: http (HTTPFetcher), browser (BrowserFetcher)",
        "Browser: not installed (rendering needs patchright; install it with: pip install 'pernix[browser]')",
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


class AsyncSetup(Addon):
    api_version = ">=1,<2"

    async def setup(self, api: object) -> None:  # pyright: ignore[reportIncompatibleMethodOverride]
        pass


def test_the_setup_of_enabled_addons_is_checked(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "-s", 'addons=["test_cli_check.AsyncSetup"]']) == 2
    assert "setup() must be synchronous" in capsys.readouterr().err


def test_every_component_problem_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["check", "-s", 'middlewares={"builtins.object": 10}', "-s", "retry.policy=builtins.object"])

    err = capsys.readouterr().err
    assert code == 2
    assert "is not an async callable" in err
    assert "retry.policy must have decide" in err


def test_project_modules_import_from_the_working_directory(tmp_path: Path) -> None:
    package = tmp_path / "myproject"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "spiders.py").write_text("from pernix import Spider\n\nclass Books(Spider):\n    pass\n")
    (package / "pipelines.py").write_text("from pernix import Pipeline\n\nclass Store(Pipeline):\n    pass\n")
    (tmp_path / "pernix.toml").write_text(
        '[pipelines]\n"myproject.pipelines.Store" = 100\n\n[fetchers]\nbrowser = false\n'  # no browser to launch
    )
    script = Path(sys.executable).with_name("pernix")

    completed = subprocess.run(  # noqa: S603  # the installed command under test
        [script, "check", "myproject.spiders"], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
    )

    assert completed.returncode == 0, completed.stderr
    assert "Pipelines: Store" in completed.stdout.splitlines()
