from __future__ import annotations

__all__ = ["AddonError", "ComponentError", "PernixError", "SettingsError"]


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
