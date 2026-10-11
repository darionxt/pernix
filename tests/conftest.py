from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest

import netguard
from loopback import LoopbackServer

if TYPE_CHECKING:
    from collections.abc import Iterator


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


@pytest.fixture
def server() -> Iterator[LoopbackServer]:
    with LoopbackServer() as srv:
        yield srv
