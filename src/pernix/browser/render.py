from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, get_args

from pernix.browser.actions import Action
from pernix.browser.page_method import PageMethod
from pernix.browser.resource_types import check_resource_types
from pernix.browser.url_globs import glob_pattern

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pernix.browser.page_method import PageCallable
    from pernix.browser.resource_types import ResourceType

__all__ = ["Render"]

_ACTION_TYPES = get_args(Action.__value__)


@dataclass(frozen=True, slots=True, init=False)
class Render:
    """How a rendered Request loads its page: what to wait for, what to do on it, what to block and capture.

    `Request(render=Render(...))` renders with these options, and `render=True` with
    none. The `setup` method runs on the page before it loads; once it has loaded,
    the browser waits for `wait_for`, runs the actions in order, runs the `action`
    method, and takes the screenshot; the Response holds what they gave in
    `response.browser`. With `keep_page`, the Callback gets the page itself, open,
    as `response.page`. Like the Request, a Render is persisted with it, its page
    methods by name.

    Attributes:
        wait_for: A CSS selector of an element to wait for, visible, before the
            actions, within `browser.action_timeout`.
        actions: The actions run on the page, in order.
        block: The resource types the page may not load; `None` for those of
            `browser.block`, which this replaces, and `()` for none. A page that
            blocks some type cannot register service workers, whose requests
            would not be judged.
        capture: URL patterns of the XHR and fetch responses to capture, as globs:
            `*` matches anything but `/`, `**` anything, and `{a,b}` either. Those
            that arrived by the time the actions were done are kept, if their
            bodies then arrive within `browser.action_timeout`; a `wait_for` on what
            a response fills in makes sure it arrived.
        screenshot: Take a screenshot of the whole page after the actions.
        setup: A Spider method run on the page before it loads, after its cookies
            are set, as `async def setup(self, page)`; the routes it adds are tried
            after those of the Fetcher, which block resource types, rewrite the
            first request, and capture responses.
        action: A Spider method run on the page once the actions are done, before
            the screenshot, as `async def action(self, page)`, for what the
            declarative `actions` cannot do.
        keep_page: Give the Callback the page, open, as `response.page`; it is
            closed, with its context, once the Callback ends, and counts toward
            `browser.max_pages` until then. Only the Callback of a Request a Run
            fetches can keep a page: an Inline Fetch cannot.

    Example:
        >>> Render(wait_for="div.item", actions=[Scroll(times=3), Click("button.more")], capture="**/api/list*")
        >>> Render(setup=self.log_in, action=PageMethod(self.close_banner, optional=True), keep_page=True)
    """

    wait_for: str | None
    actions: tuple[Action, ...]
    block: tuple[ResourceType, ...] | None
    capture: tuple[str, ...]
    screenshot: bool
    setup: PageMethod | None
    action: PageMethod | None
    keep_page: bool

    def __init__(
        self,
        *,
        wait_for: str | None = None,
        actions: Iterable[Action] = (),
        block: ResourceType | Iterable[ResourceType] | None = None,
        capture: str | Iterable[str] = (),
        screenshot: bool = False,
        setup: PageMethod | PageCallable | None = None,
        action: PageMethod | PageCallable | None = None,
        keep_page: bool = False,
    ) -> None:
        """Choose the options of a rendered Request.

        Args:
            wait_for: A CSS selector of an element to wait for.
            actions: The actions to run, in order.
            block: The resource types to block, over `browser.block`; `()` blocks
                none. Blocking `stylesheet` or `script` can keep the page from
                rendering as it should.
            capture: A URL pattern, or several, of the XHR and fetch responses to
                capture.
            screenshot: Take a screenshot after the actions.
            setup: A method to run before the page loads, or a `PageMethod` to
                mark it optional or limit its time.
            action: A method to run once the actions are done, or a `PageMethod`.
            keep_page: Give the Callback the open page.

        Raises:
            ValueError: `wait_for` or a pattern is empty, or `block` names a type
                that cannot be blocked; the message suggests the closest one.
            TypeError: An action is not one of `pernix.browser`'s, or a page method
                is not an async method.
        """
        if wait_for is not None and not _is_text(wait_for):
            raise ValueError(f"Render: wait_for must be a CSS selector, got {wait_for!r}")
        checked_actions = tuple(actions)
        for step in checked_actions:
            if not isinstance(step, _ACTION_TYPES):
                raise TypeError(
                    f"Render: {step!r} is not an action; use those of pernix.browser, such as Click or WaitFor"
                )
        try:
            checked_block = None if block is None else check_resource_types(block)
        except ValueError as exc:
            raise ValueError(f"Render(block=...): {exc}") from None
        patterns = (capture,) if isinstance(capture, str) else tuple(capture)
        for pattern in patterns:
            if not _is_text(pattern):
                raise ValueError(f"Render: a capture pattern must be a URL glob, such as '**/api/*', got {pattern!r}")
            glob_pattern(pattern)
        set_field = object.__setattr__
        set_field(self, "wait_for", wait_for)
        set_field(self, "actions", checked_actions)
        set_field(self, "block", checked_block)
        set_field(self, "capture", patterns)
        set_field(self, "screenshot", screenshot)
        set_field(self, "setup", _page_method(setup))
        set_field(self, "action", _page_method(action))
        set_field(self, "keep_page", keep_page)


def _page_method(method: PageMethod | PageCallable | None) -> PageMethod | None:
    """A page method as Render keeps it: wrapped in a `PageMethod`, which checks that it is async."""
    if method is None or isinstance(method, PageMethod):
        return method
    return PageMethod(method)


def _is_text(value: object) -> bool:
    """Whether a value is a string with more than whitespace; annotations are not enforced when constructing."""
    return isinstance(value, str) and bool(value.strip())
