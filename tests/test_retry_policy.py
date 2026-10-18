from __future__ import annotations

import datetime as dt
import random

import pytest

from pernix import Request, Response, Retry
from pernix.errors import (
    BrowserLost,
    ConnectError,
    DecodeError,
    FetchError,
    PageCrashed,
    ProxyError,
    Timeout,
    TLSError,
    TooManyRedirects,
)
from pernix.retry_policy import Action, Decision, DefaultRetryPolicy, RetryBudget, budget_spent, retry_after_seconds
from pernix.settings import RetrySettings

URL = "https://example.com/"
NOW = dt.datetime(2026, 10, 13, 12, 0, 0, tzinfo=dt.UTC)
DEFAULT_STATUSES = [408, 429, 500, 502, 503, 504, 522, 524]


class Ceiling(random.Random):
    """Jitter that always picks the largest delay, to observe the backoff ceiling."""

    def uniform(self, a: float, b: float) -> float:
        return b


def _policy(**settings: object) -> DefaultRetryPolicy:
    return DefaultRetryPolicy(RetrySettings(**settings), rng=Ceiling(), now=lambda: NOW)  # pyright: ignore[reportArgumentType]


def _response(status: int, headers: list[tuple[str, str]] | None = None) -> Response:
    return Response(url=URL, status=status, headers=headers or [], body=b"", request=Request(URL))


@pytest.mark.parametrize("status", DEFAULT_STATUSES)
def test_the_default_statuses_are_retried(status: int) -> None:
    assert _policy().decide(Request(URL), _response(status), 1) == Decision.retry(1.0, f"HTTP {status}")


@pytest.mark.parametrize("status", [200, 301, 403, 404, 501])
def test_other_statuses_are_kept(status: int) -> None:
    assert _policy().decide(Request(URL), _response(status), 1) == Decision.accept()


@pytest.mark.parametrize(
    "error", [Timeout("slow", code=28), ConnectError("refused", code=7), ProxyError("proxy"), PageCrashed("crashed")]
)
def test_transient_transport_errors_are_retried(error: FetchError) -> None:
    decision = _policy().decide(Request(URL), error, 1)

    assert (decision.action, decision.reason) == (Action.RETRY, f"{type(error).__name__}: {error}")


@pytest.mark.parametrize(
    "error",
    [TLSError("certificate"), TooManyRedirects("loop"), DecodeError("gzip"), FetchError("other"), ValueError("bug")],
)
def test_errors_that_would_happen_again_are_kept(error: Exception) -> None:
    assert _policy().decide(Request(URL), error, 1) == Decision.accept()


@pytest.mark.parametrize("attempt", [1, 4, 100])
def test_a_lost_browser_is_retried_at_once_under_its_own_budget(attempt: int) -> None:
    decision = _policy(times=3, browser_lost=5).decide(Request(URL), BrowserLost("the browser disconnected"), attempt)

    assert decision == Decision.retry(
        0.0, "BrowserLost: the browser disconnected", budget=RetryBudget("browser_lost", 5)
    )  # whatever attempt it was, as its retries take none of retry.times


def test_retry_browser_lost_0_leaves_no_retry_in_the_budget() -> None:
    decision = _policy(browser_lost=0).decide(Request(URL), BrowserLost("gone"), 1)

    assert decision.budget == RetryBudget("browser_lost", 0)


def test_a_spent_budget_is_named_in_the_reason() -> None:
    reason = budget_spent("BrowserLost: gone", RetryBudget("browser_lost", 3))

    assert reason == "BrowserLost: gone (browser_lost: 3 retries spent)"


def test_retry_uses_its_delay_or_the_backoff() -> None:
    policy = _policy()

    assert policy.decide(Request(URL), Retry("captcha", delay=7.5), 1) == Decision.retry(7.5, "captcha")
    assert policy.decide(Request(URL), Retry(), 2) == Decision.retry(2.0, "Retry")


def test_backoff_doubles_up_to_its_cap() -> None:
    policy = _policy(times=10, backoff_base=1.0, backoff_max=60.0)

    delays = [policy.decide(Request(URL), _response(503), attempt).delay for attempt in range(1, 9)]

    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0]


def test_backoff_is_full_jitter() -> None:
    policy = DefaultRetryPolicy(RetrySettings(times=10), rng=random.Random(7))  # noqa: S311  # seeded jitter

    delays = [policy.decide(Request(URL), _response(503), 4).delay for _ in range(200)]

    assert all(0.0 <= delay <= 8.0 for delay in delays)
    assert min(delays) < 1.0 < 7.0 < max(delays)


def test_huge_attempt_numbers_keep_the_backoff_finite() -> None:
    assert _policy(times=10_000).decide(Request(URL), _response(503), 5_000).delay == 60.0


@pytest.mark.parametrize(
    ("retry_after", "delay"),
    [
        ("120", 120.0),
        (" 0 ", 0.0),
        ("Tue, 13 Oct 2026 12:00:30 GMT", 30.0),
        ("Tue, 13 Oct 2026 11:59:00 GMT", 0.0),
        ("3600", 300.0),
        ("soon", 1.0),
        ("-5", 1.0),
    ],
)
def test_retry_after_sets_the_delay_up_to_its_cap(retry_after: str, delay: float) -> None:
    response = _response(503, [("Retry-After", retry_after)])

    assert _policy().decide(Request(URL), response, 1).delay == delay


def test_retries_run_out_after_the_configured_number() -> None:
    policy = _policy(times=3)

    assert [policy.decide(Request(URL), _response(503), attempt).action for attempt in (1, 2, 3, 4)] == [
        Action.RETRY,
        Action.RETRY,
        Action.RETRY,
        Action.GIVE_UP,
    ]
    assert policy.decide(Request(URL), _response(503), 4) == Decision.give_up("HTTP 503")
    assert _policy(times=0).decide(Request(URL), Retry("again"), 1) == Decision.give_up("again")


def test_the_statuses_are_configurable() -> None:
    policy = _policy(statuses=frozenset({403}))

    assert policy.decide(Request(URL), _response(403), 1).action is Action.RETRY
    assert policy.decide(Request(URL), _response(503), 1).action is Action.ACCEPT


def test_the_delay_scale_applies_to_every_delay() -> None:
    policy = _policy(delay_scale=0.0)

    assert policy.decide(Request(URL), _response(503, [("Retry-After", "60")]), 1).delay == 0.0
    assert policy.decide(Request(URL), Retry(delay=5.0), 1).delay == 0.0
    assert policy.decide(Request(URL), Timeout("slow"), 3).delay == 0.0


def test_retry_after_values() -> None:
    assert retry_after_seconds(None, now=NOW) is None
    assert retry_after_seconds("15", now=NOW) == 15.0
    assert retry_after_seconds("Tue, 13 Oct 2026 12:01:00 GMT", now=NOW) == 60.0
    assert retry_after_seconds("Tue, 13 Oct 2026 12:01:00", now=NOW) == 60.0
    assert retry_after_seconds("1.5", now=NOW) is None
