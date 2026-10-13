"""Retry policies: whether a Request whose attempt did not succeed is fetched again, and when.

The Engine asks the policy after every attempt that produced a Response, raised
while fetching, or raised `Retry`. Requests from the Frontier wait for their retry
back in the Frontier (`nack` with a delay), so they hold no concurrency slot.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pernix.errors import ConnectError, ProxyError, Retry, Timeout
from pernix.response import Response

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.request import Request
    from pernix.settings import RetrySettings

__all__ = ["Action", "Decision", "DefaultRetryPolicy", "RetryPolicy", "full_jitter", "retry_after_seconds"]

_RETRIED_ERRORS = (Timeout, ConnectError, ProxyError)
_MAX_DOUBLINGS = 64
"""Backoff stops doubling here, long past any cap, so the delay stays a finite float."""


class Action(StrEnum):
    """What to do with a Request after an attempt."""

    ACCEPT = "accept"
    """Keep the outcome: a Response goes to the Callback, an exception fails the Request."""
    RETRY = "retry"
    """Fetch the Request again after a delay."""
    GIVE_UP = "give_up"
    """The outcome called for a retry, but none is left; the Request fails."""


@dataclass(frozen=True, slots=True)
class Decision:
    """A retry policy's verdict on one attempt.

    Attributes:
        action: What to do.
        delay: Seconds to wait before the retry.
        reason: Why the attempt is retried or given up, for logs and Events.
    """

    action: Action
    delay: float = 0.0
    reason: str = ""

    @classmethod
    def accept(cls) -> Decision:
        """Keep the outcome of the attempt."""
        return cls(Action.ACCEPT)

    @classmethod
    def retry(cls, delay: float, reason: str) -> Decision:
        """Fetch the Request again.

        Args:
            delay: Seconds to wait first.
            reason: Why.

        Returns:
            The Decision.
        """
        return cls(Action.RETRY, delay, reason)

    @classmethod
    def give_up(cls, reason: str) -> Decision:
        """Fail the Request because no retry is left.

        Args:
            reason: Why the last attempt failed.

        Returns:
            The Decision.
        """
        return cls(Action.GIVE_UP, reason=reason)


class RetryPolicy(Protocol):
    """Decides whether a Request is retried; replace the default with `retry.policy`."""

    def decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
        """Judge one attempt.

        Args:
            request: The Request.
            outcome: The Response the download chain returned, or the exception it,
                or the Callback with `Retry`, raised.
            attempt: Which attempt this was, from 1.

        Returns:
            What to do next.
        """
        ...


class DefaultRetryPolicy:
    """Retries some statuses, transport errors, and `Retry`, with full-jitter backoff.

    The statuses are `retry.statuses`. The transport errors are `Timeout`,
    `ConnectError`, and `ProxyError`; others, such as an invalid certificate, would
    fail again. The delay is the one `Retry` names, else a `Retry-After` header's,
    up to `retry.retry_after_max`, else a random backoff up to `retry.backoff_base`
    doubled per retry, up to `retry.backoff_max`; `retry.delay_scale` multiplies it.
    After `retry.times` retries the Request fails.
    """

    def __init__(
        self,
        settings: RetrySettings,
        *,
        rng: random.Random | None = None,
        now: Callable[[], dt.datetime] | None = None,
    ) -> None:
        """Configure the policy.

        Args:
            settings: The retry settings.
            rng: The source of backoff jitter.
            now: The current time, for `Retry-After` dates.
        """
        self._settings = settings
        self._rng = rng or random.Random()  # noqa: S311  # jitter, not cryptography
        self._now = now or (lambda: dt.datetime.now(dt.UTC))

    def decide(self, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
        """Judge one attempt; see the class description.

        Args:
            request: The Request.
            outcome: The Response, or the exception raised.
            attempt: Which attempt this was, from 1.

        Returns:
            What to do next.
        """
        reason = self._retry_reason(outcome)
        if reason is None:
            return Decision.accept()
        if attempt > self._settings.times:
            return Decision.give_up(reason)
        return Decision.retry(self._delay(outcome, attempt) * self._settings.delay_scale, reason)

    def _retry_reason(self, outcome: Response | Exception) -> str | None:
        """Why the outcome calls for a retry, or `None` if it does not."""
        if isinstance(outcome, Response):
            return f"HTTP {outcome.status}" if outcome.status in self._settings.statuses else None
        if isinstance(outcome, Retry):
            return outcome.reason or "Retry"
        if isinstance(outcome, _RETRIED_ERRORS):
            return f"{type(outcome).__name__}: {outcome}"
        return None

    def _delay(self, outcome: Response | Exception, attempt: int) -> float:
        if isinstance(outcome, Retry) and outcome.delay is not None:
            return outcome.delay
        if isinstance(outcome, Response):
            seconds = retry_after_seconds(outcome.headers.get("Retry-After"), now=self._now())
            if seconds is not None:
                return min(seconds, self._settings.retry_after_max)
        return full_jitter(attempt, base=self._settings.backoff_base, cap=self._settings.backoff_max, rng=self._rng)


def full_jitter(attempt: int, *, base: float, cap: float, rng: random.Random) -> float:
    """Choose a backoff at random, up to `base` doubled per earlier attempt, and at most `cap`.

    Args:
        attempt: The attempt that failed, from 1.
        base: Seconds of the first backoff.
        cap: The most seconds a backoff can reach.
        rng: The source of jitter.

    Returns:
        Seconds to wait before the next attempt.
    """
    ceiling = base * 2.0 ** min(attempt - 1, _MAX_DOUBLINGS)
    return rng.uniform(0.0, min(cap, ceiling))


def retry_after_seconds(value: str | None, *, now: dt.datetime) -> float | None:
    """Read a `Retry-After` header value as seconds to wait.

    Args:
        value: The header value: a number of seconds, or an HTTP date.
        now: The current time, timezone-aware, for a date.

    Returns:
        The seconds, never negative, or `None` if the value is missing or invalid.
    """
    if value is None:
        return None
    value = value.strip()
    if value.isdecimal():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.UTC)
    return max(0.0, (when - now).total_seconds())
