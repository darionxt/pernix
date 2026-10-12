from __future__ import annotations

from typing import Annotated, Any

import msgspec

from pernix.settings.section import ComponentMap, Section
from pernix.text_decoding import codec_for

__all__ = ["CORE_SECTIONS", "CoreSettings", "EventSettings", "ResponseSettings"]


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


class ResponseSettings(Section):
    """Settings of how Responses are read.

    Attributes:
        default_encoding: The encoding of a body that declares none, is not valid
            UTF-8, and whose encoding cannot be detected. An encoding label or a
            Python codec name.
    """

    __prefix__ = "response"

    default_encoding: str = "utf-8"

    def __post_init__(self) -> None:
        try:
            codec_for(self.default_encoding)
        except LookupError:
            key = self.key("default_encoding")
            raise ValueError(f"{key}: unknown encoding {self.default_encoding!r}") from None


CORE_SECTIONS: tuple[type[Section], ...] = (CoreSettings, EventSettings, ResponseSettings)
"""The Sections every Crawler registers."""
