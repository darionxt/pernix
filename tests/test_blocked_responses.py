"""Telling Blocked Responses, and the settings of how they are handled."""

from __future__ import annotations

import pytest

from pernix import Request, Response
from pernix.blocked_responses import BlockedResponses
from pernix.errors import SettingsError
from pernix.settings import BlockedSettings, RetrySettings, SessionSettings, load_settings
from pernix.stats import Stats
from pernix.testing import make_response

URL = "https://site.example/"


def _response(status: int) -> Response:
    return make_response(URL, status=status, request=Request(URL))


def test_the_responses_found_blocked_are_counted() -> None:
    stats = Stats()
    judge = BlockedResponses(lambda response: response.status == 403, stats=stats)

    assert [judge.blocked(_response(status)) for status in (200, 403, 403)] == [False, True, True]
    assert stats.get("responses.blocked") == 2


def test_a_judgment_that_raises_is_logged_and_the_response_taken_not_to_be_blocked(
    caplog: pytest.LogCaptureFixture,
) -> None:
    def broken(response: Response) -> bool:
        raise RuntimeError("no selector")

    stats = Stats()

    assert not BlockedResponses(broken, stats=stats).blocked(_response(403))
    assert "Judging whether" in caplog.text
    assert stats.get("responses.blocked") is None


def test_403_is_blocked_retried_three_times_apart_and_three_in_a_row_retire_a_session_unless_set() -> None:
    assert (BlockedSettings().statuses, BlockedSettings().render) == (frozenset({403}), False)
    assert (RetrySettings().blocked, SessionSettings().max_blocks) == (3, 3)
    settings = load_settings(
        spider_settings={"blocked.statuses": [403, 503], "blocked.render": True, "retry.blocked": 0}
    )

    assert settings.section(BlockedSettings) == BlockedSettings(statuses=frozenset({403, 503}), render=True)
    assert settings.section(RetrySettings).blocked == 0
    with pytest.raises(SettingsError):
        load_settings(spider_settings={"sessions.max_blocks": 0})
