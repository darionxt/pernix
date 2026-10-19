"""The Pinned Pair: the browser build the browser Fetcher launches, and the Profile the HTTP Fetcher impersonates.

The Profile sets curl's TLS, HTTP/2, and request headers, which are those of the
bundled browser; the headers that name the client, its User-Agent, client hints, and
languages, are those the browser presents, recorded here for the bundled build and
made for the platform the Run is on (ADR 0010). The browser presents them in
headless mode too, as it does with a window: the browser Fetcher only takes
`HeadlessChrome` out of its User-Agent.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = [
    "BUNDLED_BROWSER",
    "PINNED_PROFILE",
    "BrowserBuild",
    "accept_language",
    "client_headers",
    "running_platform",
]

PINNED_PROFILE = "chrome150"
"""The Profile the HTTP Fetcher impersonates by default, whose TLS and HTTP/2 are those of the bundled browser."""

_UA_PLATFORMS = {
    "macOS": "Macintosh; Intel Mac OS X 10_15_7",
    "Linux": "X11; Linux x86_64",
    "Windows": "Windows NT 10.0; Win64; x64",
}
"""The platform of Chrome's reduced User-Agent, which names no OS version or CPU but these, by `sec-ch-ua-platform`."""


@dataclass(frozen=True, slots=True)
class BrowserBuild:
    """A build of Chrome, as it names itself to sites.

    Attributes:
        version: Its full version, such as `151.0.7922.34`.
        brands: The brands of its `sec-ch-ua`, in order, as `(brand, major version)`
            pairs, the GREASE brand included.
    """

    version: str
    brands: tuple[tuple[str, str], ...]

    @property
    def major(self) -> str:
        """Its major version, such as `151`."""
        return self.version.split(".", 1)[0]

    def user_agent(self, platform: str) -> str:
        """Return its User-Agent on a platform, in Chrome's reduced form.

        Args:
            platform: `macOS`, `Linux`, or `Windows`, as `running_platform()` tells.

        Returns:
            The User-Agent, such as Chrome sends it with a window.
        """
        return (
            f"Mozilla/5.0 ({_UA_PLATFORMS[platform]}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{self.major}.0.0.0 Safari/537.36"
        )

    def sec_ch_ua(self) -> str:
        """Return its `sec-ch-ua`, such as `"Chromium";v="151", "Not=A?Brand";v="99"`."""
        return ", ".join(f'"{brand}";v="{version}"' for brand, version in self.brands)


BUNDLED_BROWSER = BrowserBuild("151.0.7922.34", (("Chromium", "151"), ("Not=A?Brand", "99")))
"""The Chrome for Testing build bundled with the pinned patchright, as it names itself: by the Chromium brand alone."""


def running_platform() -> str:
    """Return the platform the Run is on, as Chrome names it in `sec-ch-ua-platform`.

    Returns:
        `macOS`, `Windows`, or `Linux`, which other systems are taken for.
    """
    if sys.platform == "darwin":
        return "macOS"
    if sys.platform == "win32":
        return "Windows"
    return "Linux"


def accept_language(languages: Sequence[str]) -> str:
    """Make the `Accept-Language` Chrome sends for the languages it presents.

    Chrome adds the base language after a regional one, unless the next is of the
    same language or the base comes earlier, then weighs each language after the
    first 0.1 less, down to 0.1.

    Args:
        languages: The languages, in order, as `navigator.languages` gives them.

    Returns:
        The header value, such as `en-US,en;q=0.9` for `en-US`.
    """
    expanded: list[str] = []
    for language, following in zip(languages, [*languages[1:], None], strict=True):
        base = language.split("-", 1)[0]
        expanded.append(language)
        if base != language and (following is None or following.split("-", 1)[0] != base):
            expanded.append(base)
    expanded = list(dict.fromkeys(expanded))  # each once, where it first comes
    weighed = [expanded[0], *(f"{tag};q=0.{max(1, 9 - index)}" for index, tag in enumerate(expanded[1:]))]
    return ",".join(weighed)


def client_headers(languages: Sequence[str], *, platform: str | None = None) -> list[tuple[str, str]]:
    """Return the headers that name the client, as the bundled browser sends them on a platform.

    Their names are cased as curl sends those of the Profile over HTTP/1.1, so that
    each replaces the Profile's own in place.

    Args:
        languages: The languages presented, as the `locale` setting names them.
        platform: The platform; the one the Run is on when `None`.

    Returns:
        `sec-ch-ua`, `sec-ch-ua-mobile`, `sec-ch-ua-platform`, `User-Agent`, and
        `Accept-Language`, in Chrome's order.
    """
    platform = platform or running_platform()
    return [
        ("sec-ch-ua", BUNDLED_BROWSER.sec_ch_ua()),
        ("sec-ch-ua-mobile", "?0"),
        ("sec-ch-ua-platform", f'"{platform}"'),
        ("User-Agent", BUNDLED_BROWSER.user_agent(platform)),
        ("Accept-Language", accept_language(languages)),
    ]
