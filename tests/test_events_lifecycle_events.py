from pernix import events
from pernix.events import Dispatch


def test_request_scheduled_is_a_fail_open_gate() -> None:
    assert events.RequestScheduled.dispatch is Dispatch.GATE
    assert events.RequestScheduled.fail_closed is False


def test_spider_closed_runs_subscribers_in_reverse() -> None:
    assert events.SpiderClosed.reverse is True


def test_other_lifecycle_events_are_observed_in_order() -> None:
    observed = [
        events.SpiderOpened,
        events.SpiderIdle,
        events.SpiderPaused,
        events.SpiderResumed,
        events.RequestDropped,
        events.RequestFailed,
        events.ResponseReceived,
        events.ItemScraped,
        events.ItemDropped,
        events.ItemError,
        events.StatsSnapshot,
    ]

    assert all(event.dispatch is Dispatch.OBSERVE and not event.reverse for event in observed)
