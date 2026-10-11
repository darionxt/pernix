from __future__ import annotations

from typing import Annotated

import msgspec

from pernix.settings.section import Section

__all__ = ["CoreSettings"]


class CoreSettings(Section):
    """Settings of the crawl loop itself.

    Attributes:
        concurrency: The maximum number of Requests processed at once.
    """

    concurrency: Annotated[int, msgspec.Meta(ge=1)] = 16
