"""The pytest plugin of Pernix, loaded as `pernix` once Pernix is installed.

- The network guard is on while each test is set up, run, and torn down, fixtures of
  every scope included, so Fetchers reach only loopback hosts, redirects included. A
  test marked `pernix_network`, or every test once the ini option `pernix_network` is
  `allow`, may reach others. A phase during which the guard refused a URL fails, even
  when the refusal happened inside a Run, which only logs a failed fetch.
- The `loopback_server` fixture gives a started `LoopbackServer`, stopped after the
  test.

`-p no:pernix` disables the plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.fetchers.network_guard import blocking_network
from pernix.testing.loopback_server import LoopbackServer

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator

    from pernix.fetchers.network_guard import NetworkGuard

_NETWORK = "pernix_network"
_NETWORK_VALUES = ("block", "allow")


def pytest_addoption(parser: pytest.Parser) -> None:
    """Register the ini option `pernix_network`."""
    parser.addini(
        _NETWORK,
        "whether Pernix Fetchers may reach hosts other than loopback ones: block (the default) or allow",
        default="block",
    )


def pytest_configure(config: pytest.Config) -> None:
    """Register the `pernix_network` marker, and check the ini option."""
    config.addinivalue_line(
        "markers", f"{_NETWORK}: let Pernix Fetchers reach hosts other than loopback ones in this test"
    )
    value = config.getini(_NETWORK)
    if value not in _NETWORK_VALUES:
        raise pytest.UsageError(f"the ini option {_NETWORK} must be 'block' or 'allow', got {value!r}")


@pytest.hookimpl(wrapper=True)
def pytest_runtest_setup(item: pytest.Item) -> Generator[None, object, object]:
    """Guard the setup of a test, its fixtures of every scope included."""
    return (yield from _guarding(item, "setting up"))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Generator[None, object, object]:
    """Guard a test."""
    return (yield from _guarding(item, "running"))


@pytest.hookimpl(wrapper=True)
def pytest_runtest_teardown(item: pytest.Item) -> Generator[None, object, object]:
    """Guard the teardown of a test, and of the fixtures that end with it."""
    return (yield from _guarding(item, "tearing down"))


def _guarding(item: pytest.Item, phase: str) -> Generator[None, object, object]:
    """Keep the Fetchers off the network during one phase of a test, unless it may use it; fail it if refused."""
    if item.config.getini(_NETWORK) == "allow" or item.get_closest_marker(_NETWORK) is not None:
        return (yield)
    with blocking_network() as guard:
        try:
            result = yield
        except pytest.exit.Exception:  # an Exception, but ending the session must stay as asked
            raise
        except (Exception, pytest.skip.Exception, pytest.fail.Exception):  # not KeyboardInterrupt either
            _fail_if_refused(guard, phase)  # a skip, say, must not hide a refused URL
            raise
    _fail_if_refused(guard, phase)
    return result


def _fail_if_refused(guard: NetworkGuard, phase: str) -> None:
    if guard.blocked:
        pytest.fail(
            f"Pernix's network guard refused {', '.join(guard.blocked)} while {phase} this test; serve "
            f"the pages from loopback_server or a FakeFetcher, or mark the test with @pytest.mark.{_NETWORK}",
            pytrace=False,
        )


@pytest.fixture
def loopback_server() -> Iterator[LoopbackServer]:
    """A started `LoopbackServer`, stopped after the test."""
    with LoopbackServer() as server:
        yield server
