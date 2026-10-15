"""The pytest plugin of Pernix, loaded as `pernix` once Pernix is installed.

- The network guard is on in every test, so Fetchers reach only loopback hosts,
  redirects included. A test marked `pernix_network`, or every test once the ini
  option `pernix_network` is `allow`, may reach others. A test during which the guard
  refused a URL fails, even when the refusal happened inside a Run, which only logs
  a failed fetch.
- The `loopback_server` fixture gives a started `LoopbackServer`, stopped after the
  test.

`-p no:pernix` disables the plugin.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

import pytest

from pernix.fetchers.network_guard import blocking_network
from pernix.testing.loopback_server import LoopbackServer

if TYPE_CHECKING:
    from collections.abc import Iterator

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


@pytest.fixture(autouse=True)
def _pernix_network_guard(request: pytest.FixtureRequest) -> Iterator[None]:
    """Keep the Fetchers off the network during the test, unless it may use it."""
    node = cast("pytest.Item", request.node)  # pyright: ignore[reportUnknownMemberType]  # untyped in pytest
    if request.config.getini(_NETWORK) == "allow" or node.get_closest_marker(_NETWORK) is not None:
        yield
        return
    with blocking_network() as guard:
        yield
    if guard.blocked:
        refused = ", ".join(guard.blocked)
        pytest.fail(
            f"Pernix's network guard refused {refused} during this test; serve the pages from "
            f"loopback_server or a FakeFetcher, or mark the test with @pytest.mark.{_NETWORK}",
            pytrace=False,
        )


@pytest.fixture
def loopback_server() -> Iterator[LoopbackServer]:
    """A started `LoopbackServer`, stopped after the test."""
    with LoopbackServer() as server:
        yield server
