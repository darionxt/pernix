"""Declarative page actions: what a rendered Request does on its page once it has loaded.

Actions are immutable msgspec Structs, persisted with the Request (ADR 0004). Each
one may take a `timeout`, in seconds, `browser.action_timeout` when `None`, and be
`optional`, so that its failure is logged and the next action runs.
"""

from __future__ import annotations

from typing import Literal

import msgspec

__all__ = ["Action", "Click", "Evaluate", "Screenshot", "Scroll", "Type", "WaitFor", "WaitState"]

type WaitState = Literal["attached", "detached", "visible", "hidden"]
"""What `WaitFor` waits for: an element in the document, gone from it, visible, or hidden or gone."""

_WAIT_STATES = frozenset({"attached", "detached", "visible", "hidden"})


class _PageAction(
    msgspec.Struct, frozen=True, kw_only=True, tag_field="action", omit_defaults=True, repr_omit_defaults=True
):
    """What every action takes: a timeout, and whether it may fail."""

    timeout: float | None = None
    optional: bool = False

    def __post_init__(self) -> None:
        if self.timeout is not None and not self.timeout > 0:
            raise ValueError(f"{type(self).__name__}: timeout must be positive, got {self.timeout}")


class WaitFor(_PageAction, frozen=True, tag="wait_for"):
    """Wait for an element to reach a state, such as content a script adds later.

    Attributes:
        selector: A CSS selector; the first element matching it counts.
        state: What to wait for, `visible` by default.
        timeout: Seconds to wait at most.
        optional: Go on with the next action when the wait fails.
    """

    selector: str
    state: WaitState = "visible"

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_selector(self, self.selector)
        if self.state not in _WAIT_STATES:
            raise ValueError(f"WaitFor: state must be one of {sorted(_WAIT_STATES)}, got {self.state!r}")


class Scroll(_PageAction, frozen=True, tag="scroll"):
    """Scroll to the bottom of the page, some times, pausing after each, as pages loading more on scroll need.

    Attributes:
        times: How many times to scroll.
        delay: Seconds to pause after each scroll, for the page to load more.
        timeout: Seconds each scroll may take, the pauses apart.
        optional: Go on with the next action when scrolling fails.
    """

    times: int = 1
    delay: float = 0.5

    def __post_init__(self) -> None:
        super().__post_init__()
        if not _is_count(self.times):
            raise ValueError(f"Scroll: times must be a positive integer, got {self.times!r}")
        if not self.delay >= 0:
            raise ValueError(f"Scroll: delay must not be negative, got {self.delay}")


class Click(_PageAction, frozen=True, tag="click"):
    """Click an element, once it can be clicked.

    Attributes:
        selector: A CSS selector; the first element matching it is clicked.
        timeout: Seconds to wait for the element at most.
        optional: Go on with the next action when the click fails, as for a
            banner that may not show.
    """

    selector: str

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_selector(self, self.selector)


class Type(_PageAction, frozen=True, tag="type"):
    """Type text into an element, key by key, after what it holds.

    The text, which may be a password, appears neither in its `repr` nor in errors.

    Attributes:
        selector: A CSS selector; the first element matching it gets the text.
        text: The text to type.
        delay: Seconds between two keys.
        timeout: Seconds to wait for the element at most.
        optional: Go on with the next action when typing fails.
    """

    selector: str
    text: str
    delay: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        _check_selector(self, self.selector)
        if not self.delay >= 0:
            raise ValueError(f"Type: delay must not be negative, got {self.delay}")

    def __repr__(self) -> str:
        return f"Type(selector={self.selector!r}, text='***')"


class Evaluate(_PageAction, frozen=True, tag="evaluate"):
    """Run JavaScript in the page; its result, made JSON-compatible, goes to `response.browser.evaluated`.

    Attributes:
        script: A JavaScript expression, or a function to call, such as
            `"() => window.__DATA__"`; a promise is awaited.
        isolated: Run it in a world of its own, which the page's scripts do not
            see, but whose globals are not the page's either; the DOM is shared.
        timeout: Seconds it may take.
        optional: Go on with the next action when it fails; it then gives no
            result.
    """

    script: str
    isolated: bool = False

    def __post_init__(self) -> None:
        super().__post_init__()
        if not _is_text(self.script):
            raise ValueError(f"Evaluate: script must be JavaScript, got {self.script!r}")


class Screenshot(_PageAction, frozen=True, tag="screenshot"):
    """Take a PNG screenshot of the page; it goes to `response.browser.screenshots`.

    Attributes:
        full_page: Take the whole page rather than what the window shows.
        timeout: Seconds it may take.
        optional: Go on with the next action when it fails; it then gives no
            screenshot.
    """

    full_page: bool = True


type Action = WaitFor | Scroll | Click | Type | Evaluate | Screenshot
"""A declarative page action."""


def _check_selector(action: _PageAction, selector: object) -> None:
    if not _is_text(selector):
        raise ValueError(f"{type(action).__name__}: selector must be a CSS selector, got {selector!r}")


def _is_text(value: object) -> bool:
    """Whether a value is a string with more than whitespace; annotations are not enforced when constructing."""
    return isinstance(value, str) and bool(value.strip())


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1
