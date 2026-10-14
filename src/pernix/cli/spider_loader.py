from __future__ import annotations

import importlib
import importlib.util
import sys
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from pernix.cli.options import UsageError
from pernix.spider import Spider

if TYPE_CHECKING:
    from types import ModuleType

__all__ = ["load_spider"]


def load_spider(target: str) -> type[Spider]:
    """Find the Spider a command names.

    The target is a `.py` file, or an import path such as `myproject.spiders.books`,
    optionally followed by `:Name`, where `Name` is a class name or a `Spider.name`.
    Without a name, the module must define exactly one Spider; Spiders it imports
    from elsewhere do not count. A file is imported under its stem, with its
    directory first on `sys.path`, so it can import the modules next to it.

    Args:
        target: The `TARGET[:Name]` given on the command line.

    Returns:
        The Spider class.

    Raises:
        UsageError: The module cannot be found or imported, or it does not define
            exactly one matching Spider.
    """
    location, name = _split(target)
    defined = _spiders_of(_load_module(location))
    matches = defined if name is None else [spider for spider in defined if name in (spider.__name__, spider.name)]
    if len(matches) == 1:
        return matches[0]
    found = ", ".join(sorted(f"{spider.__name__} (name {spider.name!r})" for spider in defined)) or "none"
    if not matches:
        missing = "no Spider" if name is None else f"no Spider named {name!r}"
        raise UsageError(f"{location} defines {missing}; Spiders defined there: {found}")
    raise UsageError(f"{location} defines several Spiders that match: {found}; choose one with {location}:Name")


def _split(target: str) -> tuple[str, str | None]:
    """Split off a `:Name` suffix, keeping Windows drive letters and paths whole."""
    location, sep, name = target.rpartition(":")
    if sep and location and name and not any(char in name for char in "/\\") and not Path(target).exists():
        return location, name
    return target, None


def _load_module(location: str) -> ModuleType:
    path = Path(location)
    if location.endswith(".py") or path.is_file():
        if not path.is_file():
            raise UsageError(f"no such file: {location}")
        return _import_file(path)
    try:
        return importlib.import_module(location)
    except ImportError as exc:
        raise UsageError(f"cannot import {location!r}: {exc}") from exc
    except Exception as exc:
        raise UsageError(f"importing {location!r} failed:\n{_traceback(exc)}") from exc


def _import_file(path: Path) -> ModuleType:
    name = path.stem
    existing = sys.modules.get(name)
    if existing is not None and Path(getattr(existing, "__file__", "") or "").resolve() != path.resolve():
        raise UsageError(f"cannot import {path}: the module name {name!r} is taken; rename the file")
    directory = str(path.resolve().parent)
    if directory not in sys.path:
        sys.path.insert(0, directory)
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise UsageError(f"cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # classes look their module up while they are created
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        raise UsageError(f"importing {path} failed:\n{_traceback(exc)}") from exc
    return module


def _spiders_of(module: ModuleType) -> list[type[Spider]]:
    return [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, Spider)
        and value is not Spider
        and value.__module__ == module.__name__
    ]


def _traceback(exc: BaseException) -> str:
    """Format an exception raised while importing, leaving out the frames of the import machinery."""
    frames = exc.__traceback__
    while frames is not None and _is_machinery(frames.tb_frame.f_code.co_filename):
        frames = frames.tb_next
    return "".join(traceback.format_exception(type(exc), exc, frames)).rstrip()


def _is_machinery(filename: str) -> bool:
    return filename == __file__ or filename.startswith("<frozen importlib")
