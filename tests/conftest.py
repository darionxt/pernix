from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import textwrap
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

import netguard
from pernix.errors import ComponentError
from pernix.fetchers import BrowserSettings
from pernix.fetchers.launched_browser import launch_browser
from pernix.logs.redaction import MASK
from pernix.proxies.proxy_url import proxy_address, proxy_credentials

if TYPE_CHECKING:
    from collections.abc import Callable, Generator, Iterator, Mapping
    from pathlib import Path

    from pernix.testing import LoopbackServer

pytest_plugins = ["pytester"]

_NETWORK_CHECK = "network_check"
"""The marker of the checks in tests/network_checks, which only a real network can make."""


@dataclass(frozen=True, slots=True)
class CheckProxy:
    """The proxy `--network-checks-proxy` names, which a check shows without its credentials.

    Attributes:
        url: The proxy's URL, credentials included, to go through.
    """

    url: str = field(repr=False)

    def __str__(self) -> str:
        return proxy_address(self.url)

    def __repr__(self) -> str:
        return f"CheckProxy({proxy_address(self.url)!r})"


def pytest_addoption(parser: pytest.Parser) -> None:
    """Add `--network-checks`, which runs the checks only a real, clean network can make, and the proxy they use."""
    parser.addoption(
        "--network-checks",
        action="store_true",
        help="run the checks of tests/network_checks, which reach real hosts; meant for a clean network",
    )
    parser.addoption(
        "--network-checks-proxy",
        metavar="URL",
        default=None,
        help="the proxy the network checks check the egress through; that check is skipped without it",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the marker of the network checks."""
    config.addinivalue_line(
        "markers", f"{_NETWORK_CHECK}: a check that reaches real hosts, run only with --network-checks"
    )


@pytest.fixture
def check_proxy(pytestconfig: pytest.Config) -> CheckProxy | None:
    """The proxy `--network-checks-proxy` names for the network checks, if any."""
    url = pytestconfig.getoption("--network-checks-proxy")
    return CheckProxy(url) if url else None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the network checks unless asked for; those asked for may reach any host."""
    run = bool(config.getoption("--network-checks"))
    skip = pytest.mark.skip(reason="a network check, which reaches real hosts; run it with --network-checks")
    for item in items:
        if item.get_closest_marker(_NETWORK_CHECK) is not None:
            item.add_marker(pytest.mark.pernix_network if run else skip)


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(
    item: pytest.Item, call: pytest.CallInfo[None]
) -> Generator[None, pytest.TestReport, pytest.TestReport]:
    """Show the proxy of the network checks without its credentials in what they report, their tracebacks included."""
    report = yield
    url = item.config.getoption("--network-checks-proxy")
    if url and call.excinfo is not None and item.get_closest_marker(_NETWORK_CHECK) is not None:
        if report.longrepr is not None and not isinstance(report.longrepr, tuple):
            report.longrepr = _without_credentials(str(report.longrepr), url)
        report.sections = [(title, _without_credentials(text, url)) for title, text in report.sections]
    return report


def _without_credentials(text: str, url: str) -> str:
    """A text with a proxy's URL in it shown without its credentials, and its credentials masked wherever else.

    A credential is masked as written, and as the `repr` of a string, or JSON,
    escapes it, as a traceback shows the values of its frames.
    """
    text = text.replace(url, proxy_address(url))
    forms = {
        form
        for credential in proxy_credentials(url)
        for form in (credential, repr(credential)[1:-1], json.dumps(credential)[1:-1])
    }
    for form in sorted(forms, key=len, reverse=True):  # the longest first, so that none is left in part
        text = text.replace(form, MASK)
    return text


_LAUNCH_TIMEOUT = 60
"""Seconds the browser may take to start for the tests, before they fail with its log."""


@pytest.fixture(autouse=True)
def _block_external_network(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests must never reach the external network, but the network checks, run only when asked for.

    This covers Python sockets only; native transports are guarded at the Fetcher level.
    """
    if request.node.get_closest_marker(_NETWORK_CHECK) is None:
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
def resolver(monkeypatch: pytest.MonkeyPatch) -> Callable[[Mapping[str, list[str]]], None]:
    """Answer the host names given with their addresses, and no other, without asking DNS."""

    def answer(addresses: Mapping[str, list[str]]) -> None:
        def getaddrinfo(host: str, port: int, *_args: Any, **_kwargs: Any) -> list[Any]:
            if host not in addresses:
                raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided, or not known")
            return [
                (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))
                for address in addresses[host]
            ]

        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)

    return answer


@pytest.fixture
def refused_proxy() -> str:
    """A proxy on a loopback port nothing listens on, so that every connection to it is refused."""
    with socket.create_server(("127.0.0.1", 0)) as listening:
        port = listening.getsockname()[1]
    return f"http://127.0.0.1:{port}"


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


@pytest.fixture
def without_patchright(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make importing patchright fail, as where `pernix[browser]` is not installed."""
    for name in list(sys.modules):
        if name == "patchright" or name.startswith("patchright."):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "patchright", None)


@pytest.fixture
def without_browser_engine(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> None:
    """Make patchright look for its browser engine in an empty directory, as where it is not installed."""
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path_factory.mktemp("no-browsers")))


@pytest.fixture(scope="session")
def browser_engine() -> None:
    """Launch the browser once, for the tests that need it.

    Where patchright or the browser engine is not installed, they are skipped, and
    fail on CI, which installs them; a browser that does not start fails them.
    """
    problem, installed = asyncio.run(_launch_once())
    if problem is None:
        return
    if installed or os.environ.get("CI"):
        pytest.fail(problem)
    pytest.skip(problem)


async def _launch_once() -> tuple[str | None, bool]:
    """Launch the browser and close it: the problem, if any, and whether the browser is installed."""
    settings = BrowserSettings(launch_options={"timeout": _LAUNCH_TIMEOUT * 1000})
    try:
        launched = await launch_browser(settings)  # under the network guard, which is on during setup
    except ComponentError as exc:
        return str(exc), False
    except Exception as exc:  # reported as the reason the tests fail
        return f"the browser does not start: {exc}", True
    await launched.close()
    return None, True
