from __future__ import annotations

import asyncio
import os
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

import netguard

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pernix.testing import LoopbackServer

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _block_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never reach the external network.

    This covers Python sockets only; native transports are guarded at the Fetcher level.
    """
    netguard.install(monkeypatch)


@pytest.fixture(autouse=True)
def _isolate_settings_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep PERNIX_* variables of the developer's shell out of the tests."""
    for name in list(os.environ):
        if name.startswith("PERNIX_"):
            monkeypatch.delenv(name)


@pytest.fixture(autouse=True)
def _restore_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo what tests, such as those running the command line, add to `sys.path`."""
    monkeypatch.setattr(sys, "path", list(sys.path))


@pytest.fixture
def server(loopback_server: LoopbackServer) -> LoopbackServer:
    """The pytest plugin's `loopback_server`, under the shorter name the suite uses."""
    return loopback_server


@pytest.fixture
def write_spider(tmp_path: Path) -> Iterator[Callable[[str, str], Path]]:
    """Write a module of Spiders to the temporary directory; the imports it causes are undone afterwards."""
    names: list[str] = []

    def write(name: str, source: str) -> Path:
        path = tmp_path / f"{name}.py"
        path.write_text(textwrap.dedent(source))
        names.append(name)
        return path

    yield write
    for name in names:
        module = sys.modules.get(name)
        if module is not None and str(getattr(module, "__file__", "")).startswith(str(tmp_path)):
            del sys.modules[name]  # never a module of the same name loaded from elsewhere


@pytest.fixture(scope="session")
def browser_engine() -> None:
    """Skip a test that needs the browser engine where it is not installed; fail it on CI, which installs it."""
    missing = _missing_browser_engine()
    if missing is not None:
        if os.environ.get("CI"):
            pytest.fail(missing)
        pytest.skip(missing)


def _missing_browser_engine() -> str | None:
    try:
        from patchright.async_api import async_playwright
    except ImportError:
        return "patchright is not installed"

    async def executable() -> str:
        async with async_playwright() as playwright:
            return playwright.chromium.executable_path

    path = asyncio.run(executable())
    return None if Path(path).exists() else f"the browser engine is not installed: patchright install chromium ({path})"
