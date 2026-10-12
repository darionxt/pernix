from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pernix.response import Response

__all__ = [
    "AddonError",
    "ComponentError",
    "ConnectError",
    "DecodeError",
    "DropItem",
    "FetchError",
    "IgnoreRequest",
    "PernixError",
    "ProxyError",
    "RetriesExhausted",
    "Retry",
    "SerializationError",
    "SettingsError",
    "TLSError",
    "Timeout",
    "TooManyRedirects",
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


class FetchError(PernixError):
    """Fetching a Request failed before a complete Response arrived.

    Fetchers raise its subclasses for the failures they recognize, and `FetchError`
    itself for the others.

    Attributes:
        code: The transport's own error code, such as libcurl's, or 0.
    """

    def __init__(self, message: str, *, code: int = 0) -> None:
        """Create the error.

        Args:
            message: What went wrong.
            code: The transport's own error code, or 0.
        """
        super().__init__(message)
        self.code = code


class Timeout(FetchError):
    """Connecting or receiving took longer than the Request's timeout."""


class ConnectError(FetchError):
    """The connection could not be made, or broke before the Response was complete."""


class ProxyError(FetchError):
    """The proxy could not be reached or refused the connection."""


class TLSError(FetchError):
    """The TLS handshake failed, for example because the certificate is invalid."""


class TooManyRedirects(FetchError):
    """The Response redirected more often than allowed."""


class DecodeError(FetchError):
    """The body could not be decoded, for example from its `Content-Encoding`."""


class RetriesExhausted(PernixError):
    """Every retry the retry policy allowed was used, and the last attempt failed too.

    For retries because of an exception, that exception is the `__cause__`.

    Attributes:
        reason: Why the last attempt failed, such as `"HTTP 503"`.
        response: The last Response, when the retries were because of its status.
    """

    def __init__(self, reason: str, *, response: Response | None = None) -> None:
        """Create the error.

        Args:
            reason: Why the last attempt failed.
            response: The last Response, if any.
        """
        super().__init__(reason)
        self.reason = reason
        self.response = response


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
    """Ask for another attempt at the current Request, from a Middleware, a Fetcher, or a Callback.

    The retry policy decides whether a retry is left; Items and Requests a Callback
    yielded before raising it are kept.

    Attributes:
        delay: Seconds to wait before the next attempt; `None` lets the policy choose.
    """

    def __init__(self, reason: str = "", *, delay: float | None = None) -> None:
        """Create the exception.

        Args:
            reason: Why, for logs and Events.
            delay: Seconds to wait before the next attempt; `None` lets the policy
                choose, with backoff.
        """
        super().__init__(reason)
        self.delay = delay


class DropItem(_ControlFlow):
    """Discard the Item being processed, from a Pipeline.

    Pipelines are not implemented yet; until they are, this is an ordinary exception.
    """
