from __future__ import annotations

from typing import Annotated, Any

import msgspec

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
    """

    __prefix__ = "browser"

    headless: bool = True
    timeout: Annotated[float, msgspec.Meta(gt=0)] = 30.0
    deadline: Annotated[float, msgspec.Meta(gt=0)] = 180.0
    launch_options: dict[str, Any] = msgspec.field(default_factory=dict[str, Any])

    def __post_init__(self) -> None:
        if "headless" in self.launch_options:
            raise ValueError(f"{self.key('launch_options')}: set 'headless' with {self.key('headless')} instead")
