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
        deadline: Seconds a whole fetch may take, starting the browser included,
            from when `max_pages` lets it open its page.
        launch_options: Options passed as they are to patchright's
            `BrowserType.launch()`, such as `executable_path` or `args`, over those
            Pernix chooses.
        max_pages: Pages open at once, each that of one fetch, at most; a fetch
            waits for one to close before it opens its own. A Run processes this
            many rendered Requests at once, apart from `concurrency`.
        generation_age: Seconds after which the browser is replaced by a new one:
            the fetches in it finish there while the next go to the new one, and it
            closes once they are done; 0 never replaces it for its age.
        generation_pages: Pages the browser opens before it is replaced, the same
            way; 0 for no limit.
        generation_memory: MiB the processes of the browser may take, their
            resident sets summed as its health probe measures them every 10 s,
            before it is replaced, the same way; 0, the default, does not measure
            it. Measuring it takes psutil, which the `browser` extra installs.
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
    max_pages: Annotated[int, msgspec.Meta(ge=1)] = 8
    generation_age: Annotated[float, msgspec.Meta(ge=0)] = 3600.0
    generation_pages: Annotated[int, msgspec.Meta(ge=0)] = 1000
    generation_memory: Annotated[int, msgspec.Meta(ge=0)] = 0
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
