from __future__ import annotations

from typing import Annotated, Any

import msgspec
from msgspec import structs

from pernix.browser.resource_types import check_resource_types
from pernix.settings import Section

__all__ = ["BrowserSettings"]


class BrowserSettings(Section):
    """Settings of the browser Fetcher.

    Attributes:
        headless: Run the browser without a window, in Chrome's new headless mode.
        timeout: Seconds a page may take to load, until its `load` event;
            `Request(timeout=...)` overrides it.
        deadline: Seconds a whole fetch may take, starting the browser included.
        launch_options: Options passed as they are to patchright's
            `BrowserType.launch()`, such as `executable_path` or `args`, over those
            Pernix chooses.
        max_idle_contexts: Browser contexts kept idle for Sessions between their
            fetches, at most; beyond that, the least recently used is closed. 0
            closes each after its fetch.
        context_idle: Seconds a context kept for a Session may stay idle before it
            is closed.
        block: The resource types no rendered page may load, such as `image` or
            `font`; none by default. `Render(block=...)` replaces them for one
            Request. Blocking `stylesheet` or `script` can keep pages from
            rendering as they should.
        action_timeout: Seconds `Render(wait_for=...)` and each page action may
            take, unless the action sets its own `timeout`.
    """

    __prefix__ = "browser"

    headless: bool = True
    timeout: Annotated[float, msgspec.Meta(gt=0)] = 30.0
    deadline: Annotated[float, msgspec.Meta(gt=0)] = 180.0
    launch_options: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])
    max_idle_contexts: Annotated[int, msgspec.Meta(ge=0)] = 8
    context_idle: Annotated[float, msgspec.Meta(gt=0)] = 300.0
    block: tuple[str, ...] = ()
    action_timeout: Annotated[float, msgspec.Meta(gt=0)] = 10.0

    def __post_init__(self) -> None:
        if "headless" in self.launch_options:
            raise ValueError(f"{self.key('launch_options')}: set 'headless' with {self.key('headless')} instead")
        try:
            structs.force_setattr(self, "block", check_resource_types(self.block))
        except ValueError as exc:
            raise ValueError(f"{self.key('block')}: {exc}") from None
