"""Running the options of a `Render` on a page: its page methods, and once it has loaded, waiting, actions, and the screenshot."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.browser import Click, Evaluate, Screenshot, Scroll, Type, WaitFor
from pernix.errors import ActionError, ActionTimeout, IgnoreRequest, PernixError, Retry

if TYPE_CHECKING:
    from patchright.async_api import Page

    from pernix.browser import Action, PageMethod, Render

__all__ = ["ActionOutputs", "run_page_method", "run_render"]

logger = logging.getLogger(__name__)

_SCROLL = "() => window.scrollTo(0, document.scrollingElement.scrollHeight)"

_CARET_AT_END = """element => {
  element.focus();
  if ("setSelectionRange" in element) {
    try { element.setSelectionRange(element.value.length, element.value.length); } catch {}  // such as type=email
  } else if (element.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(element);
    range.collapse(false);
    getSelection().removeAllRanges();
    getSelection().addRange(range);
  }
}"""
"""Focuses an element with the caret after what it holds, where typing then goes; a focused one keeps its caret."""


@dataclass(slots=True)
class ActionOutputs:
    """What the actions on a page gave.

    Attributes:
        screenshots: The PNG screenshots, in the order taken.
        evaluated: The results of the `Evaluate` actions, in order.
    """

    screenshots: list[bytes] = field(default_factory=list[bytes])
    evaluated: list[Any] = field(default_factory=list[Any])


async def run_render(page: Page, render: Render, *, action_timeout: float, url: str) -> ActionOutputs:
    """Wait for `render.wait_for`, run the actions in order and the `action` method, then take the screenshot.

    An optional action, or an optional `action` method, that fails is logged, and
    gives nothing.

    Args:
        page: The page, loaded.
        render: The options.
        action_timeout: Seconds each step may take, unless an action sets its own.
        url: The URL of the Request, for messages.

    Returns:
        The screenshots and the results of `Evaluate` actions.

    Raises:
        pernix.errors.ActionTimeout: A step that is not optional took too long.
        pernix.errors.ActionError: A step that is not optional failed otherwise.
    """
    outputs = ActionOutputs()
    if render.wait_for is not None:
        label = f"Render(wait_for={render.wait_for!r})"
        await _run(page, WaitFor(render.wait_for), outputs, action_timeout, label, url)
    for index, action in enumerate(render.actions):
        label = f"actions[{index}], {_described(action)},"
        try:
            await _run(page, action, outputs, action.timeout or action_timeout, label, url)
        except ActionError as exc:
            if not action.optional:
                raise
            logger.warning("%s; going on, as the action is optional", exc)
    if render.action is not None:
        await run_page_method(page, render.action, step=f"Render(action={render.action.name})", url=url)
    if render.screenshot:
        await _run(page, Screenshot(), outputs, action_timeout, "Render(screenshot=True)", url)
    return outputs


async def run_page_method(page: Page, method: PageMethod, *, step: str, url: str) -> None:
    """Run a page method on a page, turning what it raises into `ActionError`s that name it.

    `Retry`, `IgnoreRequest`, and the errors of Pernix, such as a `FetchError` the
    method raised, go through as they are. A method that is optional and fails
    otherwise is logged, and the fetch goes on.

    Args:
        page: The page.
        method: The page method.
        step: Names the method in messages, such as `Render(setup=Books.log_in)`.
        url: The URL of the Request, for messages.

    Raises:
        pernix.errors.ActionTimeout: The method took longer than its `timeout`, or
            one of its own calls to the browser timed out.
        pernix.errors.ActionError: The method raised otherwise.
    """
    try:
        await _call(page, method, step, url)
    except ActionError as exc:
        if not method.optional:
            raise
        logger.warning("%s; going on, as the method is optional", exc, exc_info=exc.__cause__)


async def _call(page: Page, method: PageMethod, step: str, url: str) -> None:
    from patchright.async_api import TimeoutError as PageTimeout  # imported once a browser runs

    timeout = method.timeout
    scope = asyncio.timeout(timeout)
    try:
        async with scope:
            await method.method(page)
    except (PernixError, Retry, IgnoreRequest):
        raise
    except TimeoutError as exc:
        if scope.expired():
            raise ActionTimeout(f"{step} on {url} took longer than {timeout} s") from None
        raise ActionTimeout(f"{step} on {url} timed out: {_first_line(exc)}") from exc
    except PageTimeout as exc:
        raise ActionTimeout(f"{step} on {url} timed out: {_first_line(exc)}") from exc
    except Exception as exc:
        raise ActionError(f"{step} on {url} failed: {type(exc).__name__}: {_first_line(exc)}") from exc


def _first_line(error: BaseException) -> str:
    """The first line of an error's message, as the browser's call log follows it."""
    lines = str(error).splitlines()
    return lines[0] if lines else type(error).__name__


def _described(action: Action) -> str:
    """An action as messages name it: its kind and selector, never what it types or runs, which may be secret."""
    match action:
        case WaitFor(selector=selector) | Click(selector=selector) | Type(selector=selector):
            return f"{type(action).__name__}({selector!r})"
        case _:
            return type(action).__name__


async def _run(page: Page, action: Action, outputs: ActionOutputs, seconds: float, label: str, url: str) -> None:
    """Run one action, turning the browser's errors into `ActionError`s that name it."""
    from patchright.async_api import Error as PageError  # imported once a browser runs
    from patchright.async_api import TimeoutError as PageTimeout

    try:
        await _perform(page, action, outputs, seconds)
    except (PageTimeout, TimeoutError):
        raise ActionTimeout(f"{label} on {url} took longer than {seconds} s") from None
    except PageError as exc:
        message = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
        error = ActionError(f"{label} on {url} failed: {message}")
        if isinstance(action, Type):
            raise error from None  # the browser's call log, past the first line, holds the text typed
        raise error from exc


async def _perform(page: Page, action: Action, outputs: ActionOutputs, seconds: float) -> None:
    """Do what an action says; the browser's own timeouts apply where it has them, and `asyncio.timeout` elsewhere."""
    milliseconds = seconds * 1000
    match action:
        case WaitFor(selector=selector, state=state):
            await page.wait_for_selector(selector, state=state, timeout=milliseconds)
        case Click(selector=selector):
            await page.locator(selector).first.click(timeout=milliseconds)
        case Type(selector=selector, text=text, delay=delay):
            element = page.locator(selector).first
            deadline = time.monotonic() + seconds  # finding the element and typing share the timeout
            await element.evaluate(_CARET_AT_END, timeout=milliseconds)
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError
            await element.press_sequentially(text, delay=delay * 1000, timeout=left * 1000)
        case Scroll(times=times, delay=delay):
            for _ in range(times):
                async with asyncio.timeout(seconds):
                    await page.evaluate(_SCROLL)
                await asyncio.sleep(delay)
        case Evaluate(script=script, isolated=isolated):
            async with asyncio.timeout(seconds):
                outputs.evaluated.append(await page.evaluate(script, isolated_context=isolated))
        case Screenshot(full_page=full_page):
            outputs.screenshots.append(await page.screenshot(full_page=full_page, timeout=milliseconds))
