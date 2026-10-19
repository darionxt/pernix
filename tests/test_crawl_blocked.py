"""Runs whose Responses a site refuses: the Request is retried with another Session, or rendered."""

from __future__ import annotations

import itertools
import threading
from collections import Counter
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Request, Response, Spider
from pernix.errors import RetriesExhausted
from pernix.testing import Reply, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.testing import LoopbackServer, ReceivedRequest

HTML = [("Content-Type", "text/html")]
PAGES = 12


class Site:
    """A site that tells its visitors apart by a cookie, and refuses the first after it saw it twice.

    It refuses with a 403, or, with `challenge`, with a challenge page served with
    status 200.
    """

    def __init__(self, *, challenge: bool = False) -> None:
        self.challenge = challenge
        self._visitors = itertools.count(1)
        self._visits: Counter[str] = Counter()
        self._lock = threading.Lock()

    def answer(self, request: ReceivedRequest) -> Reply:
        cookie = request.header("Cookie") or ""
        with self._lock:
            visitor = cookie.removeprefix("visitor=") if cookie.startswith("visitor=") else str(next(self._visitors))
            self._visits[visitor] += 1
            refused = visitor == "1" and self._visits[visitor] > 2
        headers = [*HTML, ("Set-Cookie", f"visitor={visitor}")]
        if refused and self.challenge:
            return Reply("<p>challenge</p>", headers=headers)
        if refused:
            return Reply("<p>refused</p>", status=403, headers=headers)
        return Reply(f"<p>visitor {visitor}</p>", headers=headers)


class Pages(Spider):
    """Fetches the pages of a site one after another, each under whichever Session the pool gives."""

    settings: ClassVar[Mapping[str, Any]] = {
        "concurrency": 1,
        "sessions.enabled": True,
        "sessions.size": 3,
        "sessions.eager": True,
    }

    def __init__(self, url: str) -> None:
        self.url = url

    async def start(self) -> AsyncIterator[Request]:
        for number in range(PAGES):
            yield Request(f"{self.url}?page={number}", priority=-number, dont_filter=True)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"text": response.css("p::text").get(), "session": response.session}


class ChallengedPages(Pages):
    """Pages, telling a challenge page served with status 200 as Blocked."""

    def is_blocked(self, response: Response) -> bool:
        return "challenge" in response.text or super().is_blocked(response)


@pytest.mark.parametrize("challenge", [False, True], ids=["403", "challenge page"])
async def test_a_session_the_site_refuses_is_retired_and_the_run_completes_with_the_others(
    server: LoopbackServer, *, challenge: bool
) -> None:
    server.route("/", handler=Site(challenge=challenge).answer)

    report = await crawl(ChallengedPages if challenge else Pages, url=server.url("/"))

    assert report.reason == "finished"
    assert len(report.items) == PAGES
    assert all(item["text"].startswith("visitor") for item in report.items)
    assert report.stats["sessions.retirements"] == 1  # after 3 Blocked Responses in a row
    assert report.stats["responses.blocked"] == 3
    assert report.stats["requests.retried_by_budget.blocked"] == 3


async def test_a_session_switch_takes_no_ordinary_retry_and_the_budget_holds(server: LoopbackServer) -> None:
    server.route("/refused", "<p>refused</p>", status=403, headers=HTML)
    errors: list[BaseException] = []

    class Refused(Spider):
        settings: ClassVar[Mapping[str, Any]] = {
            "sessions.enabled": True,
            "sessions.size": 3,
            "sessions.eager": True,
            "sessions.max_blocks": 10,
            "retry.times": 0,
            "retry.blocked": 2,
        }

        async def start(self) -> AsyncIterator[Request]:
            yield Request(server.url("/refused"), errback=self.failed)

        async def failed(self, request: Request, error: BaseException) -> None:
            errors.append(error)

    report = await crawl(Refused)

    [error] = errors
    assert isinstance(error, RetriesExhausted)
    assert "blocked: 2 retries spent" in str(error)
    assert len(server.received) == 3  # the first attempt, then a retry with each other Session
    assert report.stats["requests.retried_by_budget.blocked"] == 2
    assert report.stats["requests.failed"] == 1


@pytest.mark.usefixtures("browser_engine")
async def test_with_render_a_blocked_http_request_is_retried_rendered(server: LoopbackServer) -> None:
    answered = itertools.count()

    def answer(_request: ReceivedRequest) -> Reply:
        if next(answered) == 0:
            return Reply("<p>refused</p>", status=403, headers=HTML)
        return Reply("<p>page</p>", headers=HTML)

    server.route("/page", handler=answer)

    class Upgrading(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"blocked.render": True}

        async def start(self) -> AsyncIterator[Request]:
            yield Request(server.url("/page"))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"text": response.css("p::text").get(), "fetcher": response.fetcher}

    report = await crawl(Upgrading)

    assert report.items == [{"text": "page", "fetcher": "browser"}]
    assert report.stats["requests.retried_by_budget.blocked"] == 1
