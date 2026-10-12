from __future__ import annotations

__all__ = [
    "AddonError",
    "ComponentError",
    "DropItem",
    "IgnoreRequest",
    "PernixError",
    "Retry",
    "SerializationError",
    "SettingsError",
]


class PernixError(Exception):
    """Base class for errors raised by Pernix."""


class SettingsError(PernixError, ValueError):
    """The settings are invalid: unknown keys, wrong types, or unresolvable components.

    Attributes:
        problems: One human-readable line per problem found.
    """

    def __init__(self, problems: list[str]) -> None:
        """Create the error from every problem found during validation.

        Args:
            problems: One human-readable line per problem.
        """
        self.problems = problems
        super().__init__("Invalid settings:\n" + "\n".join(f"  - {problem}" for problem in problems))


class ComponentError(PernixError):
    """A component could not be loaded or constructed."""


class AddonError(PernixError):
    """An Addon cannot be loaded, is incompatible, or misused its setup API."""


class SerializationError(PernixError):
    """A Request cannot be persisted, or a persisted Request cannot be restored."""


class _ControlFlow(Exception):
    """Base of the exceptions that steer processing instead of reporting a failure.

    They are not `PernixError`s, so handlers of Pernix errors never catch them.

    Attributes:
        reason: Why, for logs and Events.
    """

    def __init__(self, reason: str = "") -> None:
        """Create the exception.

        Args:
            reason: Why, for logs and Events.
        """
        super().__init__(reason)
        self.reason = reason


class IgnoreRequest(_ControlFlow):
    """Drop the Request being downloaded, from download Middleware or a Fetcher.

    The Request does not count as failed and its Callback does not run;
    `RequestIgnored` is emitted with the reason.
    """


class Retry(_ControlFlow):
    """Ask for another attempt at the current Request.

    Retries are not implemented yet; until they are, this fails the Request like any
    other exception.
    """


class DropItem(_ControlFlow):
    """Discard the Item being processed, from a Pipeline.

    Pipelines are not implemented yet; until they are, this is an ordinary exception.
    """
