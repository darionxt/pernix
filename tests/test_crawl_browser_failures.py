"""Runs whose renderer, browser, or patchright driver dies midway: the Requests in flight are retried, and the Run completes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pytest

from browser_kills import HELD_PAGE, HTML, Held, browsers, drivers, kill, renderers
from pernix import Request, Response, Spider
from pernix.testing import crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    import psutil

    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")


class Pages(Spider):
    """Renders the held page and two others."""

    def __init__(self, base: str) -> None:
        self.base = base

    async def start(self) -> AsyncIterator[Request]:
        for path in ("/held", "/a", "/b"):
            yield Request(f"{self.base}{path}", render=True)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"page": response.request.url.removeprefix(self.base), "text": response.css("p::text").get()}


@pytest.mark.parametrize(
    ("victims", "retried", "lost"),
    [
        (renderers, "requests.retried", None),
        (browsers, "requests.retried_by_budget.browser_lost", "browser.lost_by_reason.disconnected"),
        (drivers, "requests.retried_by_budget.browser_lost", "browser.lost_by_reason.driver"),
    ],
    ids=["renderer", "browser", "driver"],
)
async def test_a_run_retries_the_requests_a_dead_process_failed_and_completes(
    server: LoopbackServer, victims: Callable[[], list[psutil.Process]], retried: str, lost: str | None
) -> None:
    held = Held()
    server.route("/held", HELD_PAGE, headers=HTML)
    server.route("/held.png", handler=held)
    for path in ("/a", "/b"):
        server.route(path, f"<p>{path}</p>", headers=HTML)

    running = asyncio.create_task(crawl(Pages, base=server.url("").rstrip("/")))
    await held.wait()
    kill(victims())
    held.release.set()
    report = await running

    assert report.reason == "finished"
    assert sorted(item["page"] for item in report.items) == ["/a", "/b", "/held"]
    assert report.stats["requests.failed"] == 0
    assert int(report.stats[retried]) >= 1
    if lost is None:
        assert int(report.stats["browser.page_crashes"]) >= 1  # every page whose renderer was killed
    else:
        assert report.stats[lost] == 1
        assert report.stats["browser.generations"] == 2
