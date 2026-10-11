from __future__ import annotations

from typing import Annotated, Any

import msgspec

from pernix.settings.section import ComponentMap, Section

__all__ = ["CORE_SECTIONS", "CoreSettings", "EventSettings"]


class CoreSettings(Section):
    """Settings of the crawl loop itself.

    Attributes:
        concurrency: The maximum number of Requests processed at once.
        extensions: Extensions to run, as `{extension: order}`.
        addons: Addons to enable, in order, by entry point name, import path, class,
            or object.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
    extensions: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    addons: tuple[Any, ...] = ()


class EventSettings(Section):
    """Settings of the event bus.

    Attributes:
        timeout: Seconds an asynchronous subscriber may run before it is cancelled,
            unless it was subscribed with its own timeout.
    """

    __prefix__ = "events"

    timeout: Annotated[float, msgspec.Meta(gt=0)] = 30.0


CORE_SECTIONS: tuple[type[Section], ...] = (CoreSettings, EventSettings)
"""The Sections every Crawler registers."""
