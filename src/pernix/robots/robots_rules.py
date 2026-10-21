"""What one origin's robots.txt allows, read as RFC 9309 reads it, with protego."""

from __future__ import annotations

from enum import Enum
from urllib.parse import urlsplit

from protego import Protego

__all__ = ["PARSE_LIMIT", "Access", "RobotsRules", "access", "is_robots_txt"]

PARSE_LIMIT = 500 * 1024
"""Bytes of a robots.txt parsed at most: the least RFC 9309 lets a crawler parse, 500 KiB."""

_BOM = b"\xef\xbb\xbf"


class Access(Enum):
    """What the answer to a robots.txt fetch means, as RFC 9309 names it (§2.3.1)."""

    SUCCESSFUL = "successful"
    """It was fetched: its rules apply."""
    UNAVAILABLE = "unavailable"
    """There is none for the crawler, as a 4xx tells: everything is allowed."""
    UNREACHABLE = "unreachable"
    """It cannot be told, as a 5xx or a failed fetch tells: nothing is allowed meanwhile."""


def access(status: int) -> Access:
    """Read the final status of a robots.txt fetch.

    A 429 is unreachable rather than unavailable: it asks the crawler to slow down,
    and tells nothing of the rules.

    Args:
        status: The status, after redirects.

    Returns:
        2xx successful; 429 and 5xx unreachable; anything else, such as another 4xx,
        or a 3xx that does not redirect, unavailable.
    """
    if 200 <= status < 300:
        return Access.SUCCESSFUL
    if status == 429 or status >= 500:
        return Access.UNREACHABLE
    return Access.UNAVAILABLE


def is_robots_txt(url: str) -> bool:
    """Whether a URL is that of its origin's robots.txt, which every robots.txt allows.

    Args:
        url: The URL.

    Returns:
        Whether its path is `/robots.txt`.
    """
    return urlsplit(url).path == "/robots.txt"


class RobotsRules:
    """The rules of one origin's robots.txt for one product token.

    The groups naming the token apply, or else the `*` group, matched as protego
    matches them: case-insensitively, a group applying when its name starts a word
    of the token, so the group `examplebot` applies to `ExampleBot-News`. The
    origin's `/robots.txt` is always allowed.
    """

    __slots__ = ("_allowed", "_parser", "_user_agent")

    def __init__(self, parser: Protego | None, user_agent: str = "*", *, allowed: bool = True) -> None:
        """Hold the rules.

        Args:
            parser: The parsed robots.txt; `None` for rules that allow everything,
                or nothing, as `allowed` says.
            user_agent: The product token the rules are for.
            allowed: Without a parser, whether everything is allowed.
        """
        self._parser = parser
        self._user_agent = user_agent
        self._allowed = allowed

    @classmethod
    def parse(cls, body: bytes, user_agent: str, *, cut: bool = False) -> RobotsRules:
        """Read the body of a robots.txt.

        It is read as UTF-8, as RFC 9309 has it, without a byte order mark, and
        invalid bytes replaced.

        Args:
            body: The body, `PARSE_LIMIT` bytes at most.
            user_agent: The product token the rules are for.
            cut: The body was cut at `PARSE_LIMIT`, so its last line, likely cut
                too, is left out.

        Returns:
            The rules.
        """
        text = body.removeprefix(_BOM).decode("utf-8", errors="replace")
        if cut:
            text = text.rpartition("\n")[0]
        return cls(Protego.parse(text), user_agent)

    @classmethod
    def allowing_all(cls) -> RobotsRules:
        """Return rules that allow everything, as for an origin without a robots.txt."""
        return cls(None, allowed=True)

    @classmethod
    def disallowing_all(cls) -> RobotsRules:
        """Return rules that allow nothing but `/robots.txt`, as for an origin whose robots.txt stays unreachable."""
        return cls(None, allowed=False)

    def allows(self, url: str) -> bool:
        """Whether the rules allow fetching a URL of their origin.

        Args:
            url: The URL.

        Returns:
            Whether it is allowed.
        """
        if self._parser is None:
            return self._allowed or is_robots_txt(url)
        return self._parser.can_fetch(url, self._user_agent)

    @property
    def crawl_delay(self) -> float | None:
        """The seconds the group that applies asks between two fetches (`Crawl-delay`); `None` if it asks none."""
        return None if self._parser is None else self._parser.crawl_delay(self._user_agent)

    def __repr__(self) -> str:
        if self._parser is None:
            return f"<RobotsRules {'allowing' if self._allowed else 'disallowing'} all>"
        return f"<RobotsRules for {self._user_agent!r}>"
