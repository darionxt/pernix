from __future__ import annotations

from typing import Annotated, Any

import msgspec

from pernix.settings.section import ComponentMap, NamedComponents, Section
from pernix.text_decoding import codec_for

__all__ = [
    "CORE_SECTIONS",
    "CoreSettings",
    "EventSettings",
    "FingerprintSettings",
    "FrontierSettings",
    "ResponseSettings",
]


class CoreSettings(Section):
    """Settings of the crawl loop itself.

    Attributes:
        concurrency: The maximum number of Requests processed at once.
        extensions: Extensions to run, as `{extension: order}`.
        addons: Addons to enable, in order, by entry point name, import path, class,
            or object.
        middlewares: Download Middleware, as `{middleware: order}`; lower orders are
            further out.
        spider_middlewares: Spider Middleware, as `{middleware: order}`; lower orders
            are further out.
        fetchers: Fetchers by name, as `{name: fetcher}`; `Request(fetcher=name)`
            selects one by that name.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
    extensions: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    addons: tuple[Any, ...] = ()
    middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    spider_middlewares: ComponentMap = msgspec.field(default_factory=dict[Any, int | None])
    fetchers: NamedComponents = msgspec.field(default_factory=lambda: {"http": "pernix.fetchers.HTTPFetcher"})


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


class FrontierSettings(Section):
    """Settings of the Frontier.

    Attributes:
        lease_ttl: Seconds a claimed Request stays leased without renewal; Workers
            renew their Leases every third of it while they work.
    """

    __prefix__ = "frontier"

    lease_ttl: Annotated[float, msgspec.Meta(gt=0)] = 60.0


class FingerprintSettings(Section):
    """Settings of the Fingerprints that decide which Requests are duplicates.

    Attributes:
        headers: Names of request headers whose values take part; never `Cookie`.
        sort_query: Sort query parameters by name, so their order does not matter.
        function: A replacement Fingerprint function, `(request) -> str`, as a
            callable, a class building one, or an import path; `None` for the
            default with the options above.
    """

    __prefix__ = "fingerprint"

    headers: tuple[str, ...] = ()
    sort_query: bool = True
    function: Any = None

    def __post_init__(self) -> None:
        if any(name.strip().lower() == "cookie" for name in self.headers):
            raise ValueError(
                f"{self.key('headers')}: cookies never take part in a Fingerprint (ADR 0011), "
                "so the Cookie header cannot be declared"
            )


CORE_SECTIONS: tuple[type[Section], ...] = (
    CoreSettings,
    EventSettings,
    ResponseSettings,
    FrontierSettings,
    FingerprintSettings,
)
"""The Sections every Crawler registers."""
