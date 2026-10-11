from __future__ import annotations

__all__ = ["Secret"]


class Secret:
    """A setting value that must not appear in logs, stats, or reprs.

    `str()` and `repr()` are redacted; call `reveal()` where the real value is needed.

    Example:
        >>> token = Secret("s3cr3t")
        >>> token
        Secret('**********')
        >>> token.reveal()
        's3cr3t'
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        """Wrap a sensitive value.

        Args:
            value: The value to protect.
        """
        self._value = value

    def reveal(self) -> str:
        """Return the protected value."""
        return self._value

    def __repr__(self) -> str:
        return "Secret('**********')"

    def __str__(self) -> str:
        return "**********"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Secret):
            return NotImplemented
        return self._value == other._value

    def __hash__(self) -> int:
        return hash(self._value)
