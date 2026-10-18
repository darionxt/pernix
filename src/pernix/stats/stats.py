from __future__ import annotations

__all__ = ["StatValue", "Stats"]

type StatValue = int | float | str
"""A stat value; every one can be written as JSON."""


class Stats:
    """The stats of a Run: named counters and values, kept in memory.

    Keys are dot-separated. Pernix names its segments in lowercase snake_case, except
    labels such as status codes, reasons, and error types, and no key of Pernix is
    the prefix of another, so a snapshot can be nested by its dots. Pernix reserves
    the prefixes `run`, `requests`, `downloads`, `responses`, `items`, `frontier`,
    `throttle`, `loop`, `log`, `sessions`, `proxies`, and `browser`; use your own
    prefix for your stats. Stats are not thread-safe: update them from the event
    loop.

    Example:
        >>> stats = Stats()
        >>> stats.inc("books.pages")
        >>> stats.max("books.depth", 3)
        >>> stats.snapshot()
        {'books.depth': 3, 'books.pages': 1}
    """

    __slots__ = ("_values",)

    def __init__(self) -> None:
        """Create empty stats."""
        self._values: dict[str, StatValue] = {}

    def inc(self, key: str, count: int | float = 1) -> None:
        """Add to a counter, starting from 0.

        Args:
            key: The stat.
            count: How much to add.

        Raises:
            TypeError: The stat holds a string.
        """
        current = self._values.get(key, 0)
        if isinstance(current, str):
            raise TypeError(f"stat {key!r} holds a string and cannot be incremented")
        self._values[key] = current + count

    def set(self, key: str, value: StatValue) -> None:
        """Set a value.

        Args:
            key: The stat.
            value: An int, a float, or a string.

        Raises:
            TypeError: The value is of another type.
        """
        if not _is_stat_value(value):
            raise TypeError(f"stat {key!r} must be an int, a float, or a string, got {value!r}")
        self._values[key] = value

    def max(self, key: str, value: int | float) -> None:
        """Keep the larger of the current value and `value`.

        Args:
            key: The stat.
            value: The candidate.

        Raises:
            TypeError: The stat holds a string.
        """
        current = self._values.get(key)
        if isinstance(current, str):
            raise TypeError(f"stat {key!r} holds a string and cannot be compared")
        if current is None or value > current:
            self._values[key] = value

    def get(self, key: str, default: StatValue | None = None) -> StatValue | None:
        """Return a value, or `default` if the stat was never set.

        Args:
            key: The stat.
            default: Returned for an unknown stat.

        Returns:
            The value.
        """
        return self._values.get(key, default)

    def snapshot(self) -> dict[str, StatValue]:
        """Return a copy of every stat, sorted by key."""
        return dict(sorted(self._values.items()))


def _is_stat_value(value: object) -> bool:
    return isinstance(value, int | float | str) and not isinstance(value, bool)
