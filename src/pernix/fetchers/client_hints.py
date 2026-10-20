"""Client hints: those Chrome sends once an origin asked for them with `Accept-CH`, and their values.

An origin asks for client hints with the `Accept-CH` header of the response to a
top-level navigation or form submission, a redirect included, over a potentially
trustworthy origin; Chrome keeps the hints it names, those it knows, for that origin
in place of those it asked for before, and sends them on the requests to it from then
on. A response without the header changes nothing, as does one whose header is not a
list of tokens; an empty one, or one naming no hint Chrome knows, forgets them. Chrome
keeps them with its profile, with no expiry. `sec-ch-ua`, `sec-ch-ua-mobile`, and
`sec-ch-ua-platform` go to every potentially trustworthy origin, asked for or not.

`Critical-CH` names the hints a response needs: when it names one its `Accept-CH`
asks for that the request did not send, Chrome keeps them and sends the navigation
again (`pernix.fetchers.chain_hints`).

The values are those the bundled browser sends on the platform the Run is on, as
its pages present them: the high-entropy hints of the User-Agent as the browser
Fetcher's pages give them in headless mode, its pages' viewport, device pixel ratio,
and media preferences, and the device memory of the machine, as Chrome rounds it.
The network hints, which Chrome takes from its estimate of the connection with noise
of its own for each host, are those of a fast connection with that noise. Chrome
sends the width of an image only for the images whose width the page lays out, and
`Save-Data` only when the user asks to save data, so neither is ever sent.
"""

from __future__ import annotations

import ctypes
import math
import os
import random
import re
import sys
import zlib
from functools import cache
from typing import TYPE_CHECKING

from pernix.fetchers.pinned_pair import BUNDLED_BROWSER, running_platform
from pernix.urls import is_potentially_trustworthy, url_origin

if TYPE_CHECKING:
    from collections.abc import Iterable, MutableMapping

__all__ = [
    "DEFAULT_HINTS",
    "HINTS",
    "HintValues",
    "device_memory",
    "keep_accepted",
    "parse_hints",
    "sent_hints",
]

HINTS: tuple[str, ...] = (
    "device-memory",
    "dpr",
    "width",
    "viewport-width",
    "rtt",
    "downlink",
    "ect",
    "sec-ch-ua",
    "sec-ch-ua-arch",
    "sec-ch-ua-platform",
    "sec-ch-ua-model",
    "sec-ch-ua-mobile",
    "sec-ch-ua-full-version",
    "sec-ch-ua-platform-version",
    "sec-ch-prefers-color-scheme",
    "sec-ch-ua-bitness",
    "sec-ch-viewport-height",
    "sec-ch-device-memory",
    "sec-ch-dpr",
    "sec-ch-width",
    "sec-ch-viewport-width",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-wow64",
    "save-data",
    "sec-ch-prefers-reduced-motion",
    "sec-ch-ua-form-factors",
    "sec-ch-prefers-reduced-transparency",
)
"""Every client hint Chrome knows, in the order it keeps those an origin asks for."""

DEFAULT_HINTS: tuple[str, ...] = ("sec-ch-ua", "sec-ch-ua-mobile", "sec-ch-ua-platform")
"""The client hints Chrome sends to every potentially trustworthy origin, asked for or not."""

_SENT_ORDER: tuple[str, ...] = (
    "device-memory",
    "sec-ch-device-memory",
    "dpr",
    "sec-ch-dpr",
    "viewport-width",
    "sec-ch-viewport-width",
    "sec-ch-viewport-height",
    "rtt",
    "downlink",
    "ect",
    "sec-ch-ua",
    "sec-ch-ua-mobile",
    "sec-ch-ua-full-version",
    "sec-ch-ua-arch",
    "sec-ch-ua-platform",
    "sec-ch-ua-platform-version",
    "sec-ch-ua-model",
    "sec-ch-ua-bitness",
    "sec-ch-ua-wow64",
    "sec-ch-ua-full-version-list",
    "sec-ch-ua-form-factors",
    "sec-ch-prefers-color-scheme",
    "sec-ch-prefers-reduced-motion",
    "sec-ch-prefers-reduced-transparency",
)
"""The client hints Chrome adds to the navigations it makes, in the order it adds them; it adds no other."""

_PLATFORM_VERSIONS = {"macOS": "10_15_7", "Linux": "", "Windows": "10.0"}
"""The platform version the browser's pages present in headless mode, which patchright makes of the User-Agent."""

_VIEWPORT = (1280, 720)
"""The size of the browser's pages, patchright's default, which the browser Fetcher keeps."""

_RTT_MS = 50
"""The round-trip time of the connection the network hints tell of, in milliseconds: a fast one."""

_DOWNLINK_KBPS = 10_000
"""Its downlink, in kilobits per second: as fast as Chrome tells of."""

_NOISE_STEPS = 21
"""How many multipliers Chrome's noise on the network hints picks from: 0.90 to 1.10, by 0.01."""

_DEFAULT_MEMORY_GB = 8.0
"""The device memory told of where the machine's cannot be read."""

_TOKEN = re.compile(r"[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*")
_KEY = re.compile(r"[a-z*][a-z0-9_\-.*]*")
_BARE_ITEM = re.compile(
    r'-?\d{1,15}(?:\.\d{1,3})?|"(?:[\x20\x21\x23-\x5b\x5d-\x7e]|\\[\\"])*"|:[A-Za-z0-9+/=]*:|\?[01]'
    r"|[A-Za-z*][!#$%&'*+\-.^_`|~0-9A-Za-z:/]*"
)
"""The bare items of a Structured Field (RFC 8941): a number, a string, a byte sequence, a boolean, or a token."""


def parse_hints(value: str | None) -> tuple[str, ...] | None:
    """Read the client hints an `Accept-CH` or a `Critical-CH` header names, as Chrome reads them.

    The header is a Structured Field list of tokens, which may have parameters, each
    naming a hint, in any case; tokens naming no hint Chrome knows are skipped.

    Args:
        value: The header's value, its fields joined with commas; `None` without one.

    Returns:
        The hints, each once, in the order Chrome keeps them; `None` without a
        header, or for one that is not a list of tokens, which Chrome ignores.
    """
    if value is None:
        return None
    tokens = _tokens(value)
    if tokens is None:
        return None
    named = {token.lower() for token in tokens}
    return tuple(hint for hint in HINTS if hint in named)


def sent_hints(asked: Iterable[str]) -> tuple[str, ...]:
    """Return the client hints Chrome sends to an origin that asked for some, in the order it adds them.

    Args:
        asked: The hints the origin asked for.

    Returns:
        Those Chrome adds to a navigation, with `DEFAULT_HINTS`.
    """
    wanted = {*DEFAULT_HINTS, *asked}
    return tuple(hint for hint in _SENT_ORDER if hint in wanted)


def keep_accepted(asked: MutableMapping[str, tuple[str, ...]], url: str, accept_ch: str | None) -> bool:
    """Keep the client hints a response asks for, as Chrome keeps those of a top-level navigation's.

    Args:
        asked: The hints each origin asked for, by origin, updated in place.
        url: The URL of the response.
        accept_ch: The value of its `Accept-CH` header, its fields joined with
            commas; `None` without one.

    Returns:
        Whether the hints of the response's origin changed.
    """
    hints = parse_hints(accept_ch)
    origin = url_origin(url)
    if hints is None or origin is None or not is_potentially_trustworthy(url):
        return False
    if asked.get(origin, ()) == hints:
        return False
    if hints:
        asked[origin] = hints
    else:
        asked.pop(origin, None)
    return True


class HintValues:
    """The value of each client hint, as the bundled browser sends it on a platform.

    The noise on the network hints is Chrome's: a multiplier from 0.90 to 1.10 for
    each host, picked by a salt of each instance, as Chrome picks one for each run.
    """

    def __init__(self, platform: str | None = None, *, salt: int | None = None) -> None:
        """Make the values of a platform.

        Args:
            platform: `macOS`, `Linux`, or `Windows`; the one the Run is on when
                `None`.
            salt: The salt of the noise on the network hints, from 1 to 21; a random
                one when `None`.
        """
        platform = platform or running_platform()
        memory = _number(device_memory())
        width, height = _VIEWPORT
        self._fixed = {
            "device-memory": memory,
            "sec-ch-device-memory": memory,
            "dpr": "1",
            "sec-ch-dpr": "1",
            "viewport-width": str(width),
            "sec-ch-viewport-width": str(width),
            "sec-ch-viewport-height": str(height),
            "ect": "4g",
            "sec-ch-ua-full-version": f'"{BUNDLED_BROWSER.version}"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-platform-version": f'"{_PLATFORM_VERSIONS[platform]}"',
            "sec-ch-ua-model": '""',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-wow64": "?0",
            "sec-ch-ua-full-version-list": _full_version_list(),
            "sec-ch-ua-form-factors": '"Desktop"',
            "sec-ch-prefers-color-scheme": "light",
            "sec-ch-prefers-reduced-motion": "no-preference",
            "sec-ch-prefers-reduced-transparency": "no-preference",
        }
        self._salt = random.randint(1, _NOISE_STEPS) if salt is None else salt  # noqa: S311  # noise, not a secret

    def values(self, host: str) -> dict[str, str]:
        """Return the value of each hint Chrome sends but `DEFAULT_HINTS`, for a request to a host.

        Args:
            host: The host of the request, which the noise on the network hints
                follows.

        Returns:
            The values, by hint.
        """
        multiplier = 0.9 + ((zlib.crc32(host.encode()) + self._salt) % _NOISE_STEPS) * 0.01
        rtt = _rounded(min(_RTT_MS * multiplier, 3000.0), 50)
        downlink = _rounded(min(_DOWNLINK_KBPS * multiplier, float(_DOWNLINK_KBPS)), 50) / 1000
        return self._fixed | {"rtt": str(int(rtt)), "downlink": _number(downlink)}


@cache
def device_memory() -> float:
    """Return the device memory Chrome tells of on this machine, in gigabytes.

    Chrome rounds the machine's physical memory to the nearest power of two, the
    lower one when halfway, from 2 to 32 gigabytes.

    Returns:
        The memory, such as `16.0`.
    """
    total = _physical_memory()
    if total is None:
        return _DEFAULT_MEMORY_GB
    megabytes = total // (1024 * 1024)
    lower = 1 << (megabytes.bit_length() - 1)
    upper = lower << 1
    nearest = lower if megabytes - lower <= upper - megabytes else upper
    return min(max(nearest / 1024, 2.0), 32.0)


def _physical_memory() -> int | None:
    """The machine's physical memory in bytes, as Chrome reads it; `None` where it cannot be read."""
    if sys.platform == "win32":
        return _windows_memory()
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def _windows_memory() -> int | None:
    class _MemoryStatus(ctypes.Structure):
        _fields_ = [  # the layout ctypes reads
            ("length", ctypes.c_ulong),
            ("load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = _MemoryStatus()
    status.length = ctypes.sizeof(_MemoryStatus)
    kernel = getattr(ctypes, "windll").kernel32  # noqa: B009  # Windows only
    if not kernel.GlobalMemoryStatusEx(ctypes.byref(status)):
        return None
    return int(status.total_physical)


def _full_version_list() -> str:
    """The `sec-ch-ua-full-version-list` of the bundled build: its brands with full versions, GREASE's as `99.0.0.0`."""
    versions = [
        BUNDLED_BROWSER.version if version == BUNDLED_BROWSER.major else f"{version}.0.0.0"
        for _, version in BUNDLED_BROWSER.brands
    ]
    brands = (brand for brand, _ in BUNDLED_BROWSER.brands)
    return ", ".join(f'"{brand}";v="{version}"' for brand, version in zip(brands, versions, strict=True))


def _rounded(value: float, step: int) -> float:
    """A value rounded to the nearest multiple of a step, halfway away from zero, as Chrome rounds them."""
    return math.floor(value / step + 0.5) * step


def _number(value: float) -> str:
    """A number as Chrome writes it in a header: `16`, `9.1`."""
    return f"{value:g}"


def _tokens(value: str) -> list[str] | None:
    """The tokens of a Structured Field list (RFC 8941); `None` unless it is a list of tokens, parameters allowed."""
    text = value.strip(" \t")
    tokens: list[str] = []
    position = 0
    while position < len(text):
        token = _TOKEN.match(text, position)
        if token is None:
            return None  # a string, a number, an inner list, or no item at all
        tokens.append(token.group())
        end = _parameters(text, token.end())
        if end is None:
            return None
        position = _whitespace(text, end)
        if position == len(text):
            break
        if text[position] != ",":
            return None
        position = _whitespace(text, position + 1)
        if position == len(text):
            return None  # a trailing comma
    return tokens


def _parameters(text: str, position: int) -> int | None:
    """The end of the parameters of an item, from where they start; `None` for ones that are not valid."""
    while position < len(text) and text[position] == ";":
        position += 1
        while position < len(text) and text[position] == " ":
            position += 1
        key = _KEY.match(text, position)
        if key is None:
            return None
        position = key.end()
        if position < len(text) and text[position] == "=":
            item = _BARE_ITEM.match(text, position + 1)
            if item is None:
                return None
            position = item.end()
    return position


def _whitespace(text: str, position: int) -> int:
    while position < len(text) and text[position] in " \t":
        position += 1
    return position
