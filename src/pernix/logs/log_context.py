from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Generator

__all__ = ["RunContext", "current_request_id", "current_run", "request_context", "run_context"]


@dataclass(frozen=True, slots=True)
class RunContext:
    """Identifies the Run that code is running for.

    Attributes:
        run_id: The id of the Run.
        spider: The name of its Spider.
    """

    run_id: str
    spider: str


_run: ContextVar[RunContext | None] = ContextVar("pernix_run", default=None)
_request_id: ContextVar[str | None] = ContextVar("pernix_request_id", default=None)


@contextmanager
def run_context(run: RunContext) -> Generator[None]:
    """Mark the code inside the block, and the tasks it starts, as running for a Run.

    Args:
        run: The Run.

    Yields:
        Nothing; the block runs with the Run in effect.
    """
    token = _run.set(run)
    try:
        yield
    finally:
        _run.reset(token)


@contextmanager
def request_context(request_id: str) -> Generator[None]:
    """Mark the code inside the block, and the tasks it starts, as processing a Request.

    Args:
        request_id: The Frontier's id of the Request.

    Yields:
        Nothing; the block runs with the Request in effect.
    """
    token = _request_id.set(request_id)
    try:
        yield
    finally:
        _request_id.reset(token)


def current_run() -> RunContext | None:
    """Return the Run the calling code runs for, or `None` outside a Run."""
    return _run.get()


def current_request_id() -> str | None:
    """Return the id of the Request being processed, or `None` outside one."""
    return _request_id.get()
