from __future__ import annotations

from typing import Annotated

import msgspec
from curl_cffi.requests import BrowserType

from pernix.fetchers.pinned_pair import PINNED_PROFILE
from pernix.settings import Section
from pernix.settings.suggestions import did_you_mean

__all__ = ["DEFAULT_PROFILE", "HTTPSettings", "check_profile"]

DEFAULT_PROFILE = PINNED_PROFILE
"""The Profile the HTTP Fetcher impersonates by default: a pinned Chrome version, that of the Pinned Pair (ADR 0010)."""

_PROFILES = frozenset(browser.value for browser in BrowserType)


def check_profile(name: str) -> str:
    """Check that a Profile names a pinned impersonation target.

    Args:
        name: The Profile, such as `"chrome150"`.

    Returns:
        `name`.

    Raises:
        ValueError: `name` is an alias that drifts as curl-cffi is upgraded, such as
            `"chrome"`, or no target at all.
    """
    if name in _PROFILES:
        return name
    if not any(char.isdigit() for char in name):
        raise ValueError(
            f"the Profile {name!r} drifts as curl-cffi is upgraded; pin a version, such as {DEFAULT_PROFILE!r}"
        )
    raise ValueError(f"unknown Profile {name!r}{did_you_mean(name, _PROFILES)}")


class HTTPSettings(Section):
    """Settings of the HTTP Fetcher.

    Attributes:
        impersonate: The Profile to impersonate, a pinned browser version such as
            `"chrome150"`; `Request(impersonate=...)` overrides it. With the Profile
            of the Pinned Pair, the default, the headers that name the client are
            those of the bundled browser; another sends its own, and a Run that
            sets one is warned that the two sides then differ.
        timeout: Seconds to connect, and seconds without receiving any data before a
            transfer fails, to the nearest second; `Request(timeout=...)` overrides it.
        deadline: Seconds a whole fetch may take, waiting for a connection included.
        max_redirects: Redirects followed at most before `TooManyRedirects`.
        max_body_size: Bytes a body may have at most before `ResponseTooLarge`; 0 sets
            no limit.
        pool_size: curl sessions kept at most, one per Profile; the least recently
            used is closed first.
        pool_idle: Seconds a curl session is kept unused before it is closed.
    """

    __prefix__ = "http"

    impersonate: str = DEFAULT_PROFILE
    timeout: Annotated[float, msgspec.Meta(gt=0)] = 30.0
    deadline: Annotated[float, msgspec.Meta(gt=0)] = 180.0
    max_redirects: Annotated[int, msgspec.Meta(ge=0)] = 20
    max_body_size: Annotated[int, msgspec.Meta(ge=0)] = 100 * 1024 * 1024
    pool_size: Annotated[int, msgspec.Meta(ge=1)] = 8
    pool_idle: Annotated[float, msgspec.Meta(gt=0)] = 300.0

    def __post_init__(self) -> None:
        try:
            check_profile(self.impersonate)
        except ValueError as exc:
            raise ValueError(f"{self.key('impersonate')}: {exc}") from None
