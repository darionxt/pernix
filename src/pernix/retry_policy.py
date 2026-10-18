"""Retry policies: whether a Request whose attempt did not succeed is fetched again, and when.

The Engine asks the policy after every attempt that produced a Response, raised
while fetching, or raised `Retry`. Requests from the Frontier wait for their retry
back in the Frontier (`nack` with a delay), so they hold no concurrency slot.

A retry counts as an attempt, which the policy is told of, unless it names a
`RetryBudget`: for failures that are not the Request's own, such as a browser that
went away, a policy retries under a budget that counts those retries apart, and
takes nothing from the attempts. The Frontier keeps the count of each budget with
the Request (`Lease.spent`), as it keeps its attempts, and the Engine, or the
Inline Fetch, gives up once a retry would go past the budget's limit, failing the
Request with `RetriesExhausted`.
"""

from __future__ import annotations

import datetime as dt
import logging
import random
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from pernix.errors import BrowserLost, ConnectError, PageCrashed, ProxyError, Retry, Timeout
from pernix.response import Response

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.request import Request
    from pernix.settings import RetrySettings

__all__ = [
    "BROWSER_LOST",
    "Action",
    "Decision",
    "DefaultRetryPolicy",
    "RetryBudget",
    "RetryPolicy",
    "budget_spent",
    "consult",
    "full_jitter",
    "retry_after_seconds",
]

logger = logging.getLogger(__name__)

_RETRIED_ERRORS = (Timeout, ConnectError, ProxyError, PageCrashed)

BROWSER_LOST = "browser_lost"
"""The retry budget of the Requests whose browser went away; `retry.browser_lost` is its limit."""
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
class RetryBudget:
    """Retries a Request may have for one kind of failure, counted apart from its attempts.

    Attributes:
        name: Names the budget, such as `"browser_lost"`; the retries under one
            name are counted together, as `Lease.spent[name]`.
        limit: Retries under the budget a Request may have; 0 for none.
    """

    name: str
    limit: int


@dataclass(frozen=True, slots=True)
class Decision:
    """A retry policy's verdict on one attempt.

    Attributes:
        action: What to do.
        delay: Seconds to wait before the retry.
        reason: Why the attempt is retried or given up, for logs and Events.
        budget: The budget a retry counts against instead of the attempts, if any.
    """

    action: Action
    delay: float = 0.0
    reason: str = ""
    budget: RetryBudget | None = None

    @classmethod
    def accept(cls) -> Decision:
        """Keep the outcome of the attempt."""
        return cls(Action.ACCEPT)

    @classmethod
    def retry(cls, delay: float, reason: str, *, budget: RetryBudget | None = None) -> Decision:
        """Fetch the Request again.

        Args:
            delay: Seconds to wait first.
            reason: Why.
            budget: Count the retry against this budget instead of as an attempt;
                once the Request has had `budget.limit` retries under it, it fails
                with `RetriesExhausted` instead.

        Returns:
            The Decision.
        """
        return cls(Action.RETRY, delay, reason, budget)

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


def budget_spent(reason: str, budget: RetryBudget) -> str:
    """Tell why a Request fails once its retries under a budget are spent.

    Args:
        reason: Why its last attempt failed.
        budget: The budget.

    Returns:
        The reason, naming the budget and its limit, as for `RetriesExhausted`.
    """
    return f"{reason} ({budget.name}: {budget.limit} retries spent)"


def consult(policy: RetryPolicy, request: Request, outcome: Response | Exception, attempt: int) -> Decision:
    """Ask a retry policy to judge an attempt; a policy that raises keeps the outcome.

    Args:
        policy: The retry policy.
        request: The Request.
        outcome: The Response, or the exception raised.
        attempt: Which attempt this was, from 1.

    Returns:
        The policy's Decision; `Decision.accept()` if the policy raised, which is
        logged.
    """
    try:
        return policy.decide(request, outcome, attempt)
    except Exception:
        logger.exception("The retry policy failed to judge %r; keeping the outcome", request)
        return Decision.accept()


class DefaultRetryPolicy:
    """Retries some statuses, transport errors, and `Retry`, with full-jitter backoff.

    The statuses are `retry.statuses`. The transport errors are `Timeout`,
    `ConnectError`, `ProxyError`, and `PageCrashed`; others, such as an invalid
    certificate, would fail again. The delay is the one `Retry` names, else a
    `Retry-After` header's, up to `retry.retry_after_max`, else a random backoff up
    to `retry.backoff_base` doubled per retry, up to `retry.backoff_max`;
    `retry.delay_scale` multiplies it. After `retry.times` retries the Request
    fails.

    `BrowserLost` is retried at once, under the `browser_lost` budget, up to
    `retry.browser_lost` times, which take none of the retries of `retry.times`.
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
        if isinstance(outcome, BrowserLost):
            budget = RetryBudget(BROWSER_LOST, self._settings.browser_lost)
            return Decision.retry(0.0, f"BrowserLost: {outcome}", budget=budget)
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
