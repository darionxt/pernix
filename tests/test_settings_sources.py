from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.errors import SettingsError
from pernix.settings.sources import find_project_file, read_environment

if TYPE_CHECKING:
    from pathlib import Path


def test_pernix_toml_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("concurrency = 4\n[domain]\ndelay = 0.5\n")
    nested = tmp_path / "spiders" / "news"
    nested.mkdir(parents=True)

    project = find_project_file(nested)

    assert project is not None
    assert project.path == (tmp_path / "pernix.toml").resolve()
    assert project.values == {"concurrency": 4, "domain": {"delay": 0.5}}


def test_pyproject_tool_table_is_used_when_there_is_no_pernix_toml(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n[tool.pernix]\nconcurrency = 2\n')

    project = find_project_file(tmp_path)

    assert project is not None
    assert project.values == {"concurrency": 2}


def test_pernix_toml_wins_over_pyproject_in_the_same_directory(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[tool.pernix]\nconcurrency = 2\n")
    (tmp_path / "pernix.toml").write_text("concurrency = 3\n")

    project = find_project_file(tmp_path)

    assert project is not None
    assert project.values == {"concurrency": 3}


def test_nearest_pyproject_ends_the_search(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("concurrency = 3\n")
    package = tmp_path / "package"
    package.mkdir()
    (package / "pyproject.toml").write_text('[project]\nname = "package"\n')

    project = find_project_file(package)

    assert project is not None
    assert project.path.name == "pyproject.toml"
    assert project.values == {}


def test_invalid_toml_is_a_settings_error(tmp_path: Path) -> None:
    (tmp_path / "pernix.toml").write_text("concurrency = \n")

    with pytest.raises(SettingsError, match=r"pernix\.toml is not valid TOML"):
        find_project_file(tmp_path)


def test_environment_variables_map_to_dotted_keys() -> None:
    environ = {"PERNIX_CONCURRENCY": "8", "PERNIX_DOMAIN__DELAY": "0.5", "PERNIX_": "x", "HOME": "/home/me"}

    assert read_environment(environ) == {
        "concurrency": ("8", "PERNIX_CONCURRENCY"),
        "domain.delay": ("0.5", "PERNIX_DOMAIN__DELAY"),
    }
