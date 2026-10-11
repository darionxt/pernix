from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING, Any

from pernix.errors import SettingsError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = ["ENVIRONMENT_PREFIX", "ProjectFile", "find_project_file", "read_environment"]

ENVIRONMENT_PREFIX = "PERNIX_"


class ProjectFile:
    """Settings read from `pernix.toml`, or from `[tool.pernix]` in `pyproject.toml`.

    Attributes:
        path: The file the settings were read from.
        values: The settings table, possibly nested.
    """

    __slots__ = ("path", "values")

    def __init__(self, path: Path, values: Mapping[str, Any]) -> None:
        """Hold the settings of one project file.

        Args:
            path: The file the settings were read from.
            values: The settings table.
        """
        self.path = path
        self.values = values

    @property
    def origin(self) -> str:
        """A description of the file for error messages."""
        return f"{self.path.name} ({self.path})"


def find_project_file(start: Path) -> ProjectFile | None:
    """Find the project settings closest to a directory.

    Walks from `start` up to the filesystem root. In each directory, `pernix.toml` wins;
    otherwise a `pyproject.toml` ends the search, with its `[tool.pernix]` table as
    the settings (possibly empty).

    Args:
        start: The directory to start from, usually the working directory.

    Returns:
        The settings found, or `None` if no project file exists.

    Raises:
        SettingsError: A project file is not valid TOML.
    """
    for directory in (start.resolve(), *start.resolve().parents):
        pernix_toml = directory / "pernix.toml"
        if pernix_toml.is_file():
            return ProjectFile(pernix_toml, _read_toml(pernix_toml))
        pyproject = directory / "pyproject.toml"
        if pyproject.is_file():
            return ProjectFile(pyproject, _read_toml(pyproject).get("tool", {}).get("pernix", {}))
    return None


def read_environment(environ: Mapping[str, str]) -> dict[str, tuple[str, str]]:
    """Collect settings from `PERNIX_*` environment variables.

    `PERNIX_DOMAIN__DELAY` maps to the key `domain.delay`: the prefix is dropped,
    double underscores become dots, and the result is lower-cased.

    Args:
        environ: The environment, usually `os.environ`.

    Returns:
        For each key, its raw string value and the variable it came from.
    """
    settings: dict[str, tuple[str, str]] = {}
    for name, value in environ.items():
        if name.startswith(ENVIRONMENT_PREFIX) and len(name) > len(ENVIRONMENT_PREFIX):
            key = name.removeprefix(ENVIRONMENT_PREFIX).lower().replace("__", ".")
            settings[key] = (value, name)
    return settings


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as file:
            return tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise SettingsError([f"{path} is not valid TOML: {exc}"]) from exc
