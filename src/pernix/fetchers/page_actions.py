"""Running the options of a `Render` on a page once it has loaded: waiting, actions, and the screenshot."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.browser import Click, Evaluate, Screenshot, Scroll, Type, WaitFor
from pernix.errors import ActionError, ActionTimeout

if TYPE_CHECKING:
    from patchright.async_api import Page

    from pernix.browser import Action, Render

__all__ = ["ActionOutputs", "run_render"]

logger = logging.getLogger(__name__)

_SCROLL = "() => window.scrollTo(0, document.scrollingElement.scrollHeight)"


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
    """Wait for `render.wait_for`, run the actions in order, then take the screenshot.

    An optional action that fails is logged, and gives nothing.

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
    if render.screenshot:
        await _run(page, Screenshot(), outputs, action_timeout, "Render(screenshot=True)", url)
    return outputs


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
        raise ActionError(f"{label} on {url} failed: {message}") from exc


async def _perform(page: Page, action: Action, outputs: ActionOutputs, seconds: float) -> None:
    """Do what an action says; the browser's own timeouts apply where it has them, and `asyncio.timeout` elsewhere."""
    milliseconds = seconds * 1000
    match action:
        case WaitFor(selector=selector, state=state):
            await page.wait_for_selector(selector, state=state, timeout=milliseconds)
        case Click(selector=selector):
            await page.locator(selector).first.click(timeout=milliseconds)
        case Type(selector=selector, text=text, delay=delay):
            await page.locator(selector).first.press_sequentially(text, delay=delay * 1000, timeout=milliseconds)
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
