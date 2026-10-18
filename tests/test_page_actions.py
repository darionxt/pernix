"""Running page actions on a stand-in page: what their errors carry."""

from __future__ import annotations

import traceback
from typing import TYPE_CHECKING, Any, cast

import pytest

from pernix.browser import Evaluate, Render, Type
from pernix.errors import ActionError
from pernix.fetchers.page_actions import run_render

if TYPE_CHECKING:
    from patchright.async_api import Page

page_errors = pytest.importorskip("patchright.async_api")

CALL_LOG = (
    'Locator.press_sequentially: Element is not attached to the DOM\nCall log:\n  - elementHandle.type("hunter2")'
)


class FailingPage:
    """A page whose every step fails with a browser error carrying a call log."""

    def __init__(self, error: Exception) -> None:
        self.first = self
        self._error = error

    def locator(self, _selector: str) -> FailingPage:
        return self

    async def evaluate(self, *_args: Any, **_kwargs: Any) -> Any:
        raise self._error

    async def press_sequentially(self, *_args: Any, **_kwargs: Any) -> None:
        raise self._error


async def _failed(action: Type | Evaluate, error: Exception) -> ActionError:
    with pytest.raises(ActionError) as raised:
        await run_render(cast("Page", FailingPage(error)), Render(actions=[action]), action_timeout=1, url="https://x/")
    return raised.value


async def test_a_failed_type_carries_nothing_of_the_text_it_typed() -> None:
    error = await _failed(Type("#password", "hunter2"), page_errors.Error(CALL_LOG))

    printed = "".join(traceback.format_exception(error))
    assert "Element is not attached to the DOM" in str(error)
    assert "hunter2" not in printed
    assert error.__cause__ is None


async def test_other_failed_actions_keep_the_browsers_error_as_their_cause() -> None:
    cause = page_errors.Error("Page.evaluate: Error: boom\n    at <anonymous>:1:7")

    error = await _failed(Evaluate("() => { throw new Error('boom') }"), cause)

    assert error.__cause__ is cause
