"""Runs that obey robots.txt (`robots.obey`)."""

from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING, Any

import pytest

from pernix import IgnoreRequest, Middleware, Request, Response, Spider, events
from pernix.robots import PARSE_LIMIT
from pernix.testing import FakeFetcher, LoopbackServer, Reply, crawl, make_response

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from pernix.crawler import Crawler
    from pernix.middlewares import Download
    from pernix.testing import Answer, ReceivedRequest

SITE = "https://site.example"
OTHER = "https://other.example"
ROBOTS_TXT = "User-agent: *\nDisallow: /private\n"
OBEY = {"robots.obey": True}


class Pages(Spider):
    name = "pages"

    def __init__(self, urls: list[str], render: bool = False, obey: bool = True) -> None:
        self.urls = urls
        self.render = render
        self.obey = obey

    async def start(self) -> AsyncIterator[Request]:
        for url in self.urls:
            yield Request(url, render=self.render, ext={} if self.obey else {"robots": {"obey": False}})

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"url": response.url}


class Ignored:
    """Records the reasons of the Requests ignored."""

    def __init__(self) -> None:
        self.reasons: list[str] = []

    @events.on(events.RequestIgnored)
    def ignored(self, event: events.RequestIgnored) -> None:
        self.reasons.append(event.reason)


def _site(robots_txt: Answer | list[Answer] = ROBOTS_TXT) -> FakeFetcher:
    """SITE, whose robots.txt answers with `robots_txt`, and whose other pages with a page each."""
    return FakeFetcher({f"{SITE}/robots.txt": robots_txt, re.compile(rf"{re.escape(SITE)}/.*"): "<p>page</p>"})


def _status(status: int) -> Answer:
    return lambda request: make_response(request.url, status=status, request=request)


def _urls(report_items: list[Any]) -> list[str]:
    return sorted(item["url"] for item in report_items)


def _fetched(site: FakeFetcher) -> list[str]:
    return [request.url for request in site.requests]


async def test_off_by_default_no_robots_txt_is_fetched() -> None:
    site = _site()

    report = await crawl(Pages, fetcher=site, urls=[f"{SITE}/a", f"{SITE}/private"])

    assert _urls(report.items) == [f"{SITE}/a", f"{SITE}/private"]
    assert f"{SITE}/robots.txt" not in _fetched(site)
    assert "robots.fetched" not in report.stats


@pytest.mark.parametrize("render", [False, True])
async def test_a_disallowed_url_is_not_fetched_and_an_allowed_one_is(render: bool) -> None:
    site = _site()
    ignored = Ignored()

    report = await crawl(
        Pages,
        fetcher=site,
        browser=site if render else None,
        settings={**OBEY, "extensions": {ignored: 200}},
        urls=[f"{SITE}/a", f"{SITE}/private"],
        render=render,
    )

    assert _urls(report.items) == [f"{SITE}/a"]
    assert sorted(_fetched(site)) == [f"{SITE}/a", f"{SITE}/robots.txt"]
    assert site.requests[0].fetcher == "http"  # robots.txt, rendered Requests' included
    assert ignored.reasons == ["robots.txt"]
    assert (report.stats["robots.disallowed"], report.stats["requests.ignored"], report.stats["requests.failed"]) == (
        1,
        1,
        0,
    )


async def test_concurrent_requests_to_an_origin_fetch_robots_txt_once() -> None:
    async def slow(request: Request) -> str:
        await asyncio.sleep(0.01)  # the other Requests reach it meanwhile, or find it kept
        return ROBOTS_TXT

    site = _site(slow)

    report = await crawl(Pages, fetcher=site, settings=OBEY, urls=[f"{SITE}/{n}" for n in range(6)])

    assert len(report.items) == 6
    assert _fetched(site).count(f"{SITE}/robots.txt") == 1
    assert report.stats["robots.fetched"] == 1


@pytest.mark.parametrize("status", [404, 403, 401])
async def test_a_4xx_allows_everything(status: int) -> None:
    report = await crawl(Pages, fetcher=_site(_status(status)), settings=OBEY, urls=[f"{SITE}/private"])

    assert _urls(report.items) == [f"{SITE}/private"]


async def test_a_5xx_holds_requests_back_until_robots_txt_is_fetched_again() -> None:
    site = _site([_status(503), ROBOTS_TXT])

    report = await crawl(Pages, fetcher=site, settings=OBEY, urls=[f"{SITE}/a", f"{SITE}/private"])

    assert _urls(report.items) == [f"{SITE}/a"]
    assert _fetched(site).count(f"{SITE}/robots.txt") == 2
    assert report.stats["robots.deferred"] == 1
    assert (report.stats["requests.retried"], report.stats["requests.failed"]) == (0, 0)


async def test_a_robots_txt_that_stays_unreachable_disallows_the_origin() -> None:
    site = _site(_status(500))

    report = await crawl(Pages, fetcher=site, settings=OBEY, urls=[f"{SITE}/a", f"{SITE}/b"])

    assert report.items == []
    assert _fetched(site) == [f"{SITE}/robots.txt"] * 3
    assert (report.stats["requests.ignored"], report.stats["requests.failed"], report.reason) == (2, 0, "finished")


class Inline(Spider):
    name = "inline"
    start_urls = (f"{OTHER}/",)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        try:
            api = await self.fetch(f"{SITE}/private/api")
        except IgnoreRequest as exc:
            yield {"ignored": exc.reason}
        else:
            yield {"body": api.text}
        yield {"page": (await self.fetch(f"{SITE}/a")).text}


async def test_an_inline_fetch_robots_txt_disallows_raises_ignore_request() -> None:
    site = _site()
    site.add(f"{OTHER}/robots.txt", _status(404))
    site.add(f"{OTHER}/", "<p>other</p>")

    report = await crawl(Inline, fetcher=site, settings=OBEY)

    assert report.items == [{"ignored": "robots.txt"}, {"page": "<p>page</p>"}]
    assert f"{SITE}/private/api" not in _fetched(site)


async def test_an_inline_fetch_waits_in_place_for_an_unreachable_robots_txt() -> None:
    site = _site([_status(503), "User-agent: *\n"])
    site.add(f"{OTHER}/robots.txt", _status(404))
    site.add(f"{OTHER}/", "<p>other</p>")

    report = await crawl(Inline, fetcher=site, settings=OBEY)

    assert report.items == [{"body": "<p>page</p>"}, {"page": "<p>page</p>"}]
    assert report.stats["robots.deferred"] == 1


async def test_robots_txt_is_fetched_under_the_session_of_the_request_and_never_judged() -> None:
    site = _site(_status(403))  # a Blocked status, were it judged

    report = await crawl(Pages, fetcher=site, settings={**OBEY, "sessions.enabled": True}, urls=[f"{SITE}/a"])

    robots, page = site.requests
    assert robots.url == f"{SITE}/robots.txt"
    assert robots.session is not None
    assert robots.session == page.session
    assert _urls(report.items) == [f"{SITE}/a"]
    assert (report.stats.get("responses.blocked", 0), report.stats.get("sessions.retirements", 0)) == (0, 0)


class Authorize(Middleware):
    """Fetches a token from the origin of each Request first, as some APIs need."""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler

    async def __call__(self, request: Request, call_next: Download) -> Response:
        if request.url.endswith("/token"):
            return await call_next(request)
        token = await self.crawler.fetch(f"{SITE}/token")
        return await call_next(request.replace(headers=[*request.headers, ("Authorization", token.text)]))


async def test_a_fetch_a_middleware_makes_as_part_of_fetching_robots_txt_does_not_wait_for_it() -> None:
    site = FakeFetcher(
        {
            f"{SITE}/robots.txt": ROBOTS_TXT,
            f"{SITE}/token": "t0ken",
            re.compile(rf"{re.escape(SITE)}/.*"): "<p>page</p>",
        }
    )
    settings = {**OBEY, "domain.concurrency": 1, "middlewares": {Authorize: 100}}

    async with asyncio.timeout(5):  # the token fetch shares the slot the page fetch holds
        report = await crawl(Pages, fetcher=site, settings=settings, urls=[f"{SITE}/a"])

    assert _urls(report.items) == [f"{SITE}/a"]
    assert _fetched(site) == [f"{SITE}/token", f"{SITE}/robots.txt", f"{SITE}/token", f"{SITE}/a"]
    assert [request.headers.get("Authorization") for request in site.requests] == [None, "t0ken", None, "t0ken"]


async def test_a_crawl_delay_longer_than_the_delay_slows_the_domain_down() -> None:
    started: dict[str, float] = {}

    def at(body: str) -> Answer:
        def answer(request: Request) -> str:
            started[request.url] = time.monotonic()
            return body

        return answer

    site = FakeFetcher(
        {f"{SITE}/robots.txt": at("User-agent: *\nCrawl-delay: 0.2\n"), re.compile(rf"{re.escape(SITE)}/.*"): at("")}
    )
    settings = {**OBEY, "robots.crawl_delay_max": None}

    await crawl(Pages, fetcher=site, settings=settings, urls=[f"{SITE}/a", f"{SITE}/b"])

    starts = sorted(started.values())
    assert starts[0] == started[f"{SITE}/robots.txt"]
    assert starts[2] - starts[0] >= 0.35  # two Crawl-delays: robots.txt took a start of the domain too


async def test_a_robots_txt_reached_through_more_than_five_redirects_allows_everything(
    server: LoopbackServer,
) -> None:
    server.route("/robots.txt", status=302, headers=[("Location", "/r1")])
    for n in range(1, 7):
        server.route(f"/r{n}", status=302, headers=[("Location", f"/r{n + 1}")])
    server.route("/r7", "User-agent: *\nDisallow: /\n")
    server.route("/private", "<p>private</p>")

    report = await crawl(Pages, settings=OBEY, urls=[server.url("/private")])

    assert _urls(report.items) == [server.url("/private")]
    assert [hop.path for hop in server.received] == ["/robots.txt", "/r1", "/r2", "/r3", "/r4", "/r5", "/private"]


async def test_a_robots_txt_reached_within_five_redirects_applies(server: LoopbackServer) -> None:
    server.route("/robots.txt", status=301, headers=[("Location", "/r1")])
    for n in range(1, 5):
        server.route(f"/r{n}", status=302, headers=[("Location", f"/r{n + 1}")])
    server.route("/r5", "User-agent: *\nDisallow: /private\n")

    report = await crawl(Pages, settings=OBEY, urls=[server.url("/private")])

    assert report.items == []
    assert report.stats["robots.disallowed"] == 1


async def test_only_the_first_500_kib_of_robots_txt_are_parsed(server: LoopbackServer) -> None:
    head = "User-agent: *\nDisallow: /private\n"
    server.route("/robots.txt", head + "#" * PARSE_LIMIT + "\nDisallow: /beyond\n")
    for path in ("/private", "/beyond"):
        server.route(path, "<p>page</p>")

    report = await crawl(Pages, settings=OBEY, urls=[server.url("/private"), server.url("/beyond")])

    assert _urls(report.items) == [server.url("/beyond")]


async def test_the_cookies_robots_txt_sets_are_kept_in_the_session(server: LoopbackServer) -> None:
    server.route("/robots.txt", "User-agent: *\n", headers=[("Set-Cookie", "visited=1; Path=/")])
    server.route("/a", "<p>a</p>")

    await crawl(Pages, settings={**OBEY, "sessions.enabled": True, "sessions.size": 1}, urls=[server.url("/a")])

    robots, page = server.received
    assert (robots.path, robots.header("Cookie"), robots.header("Referer")) == ("/robots.txt", None, None)
    assert (page.path, page.header("Cookie")) == ("/a", "visited=1")


HTML = [("Content-Type", "text/html")]


@pytest.fixture
def other_server() -> Iterator[LoopbackServer]:
    with LoopbackServer() as server:
        yield server


@pytest.fixture(params=[False, True], ids=["http", "rendered"])
def render(request: pytest.FixtureRequest) -> bool:
    """Fetch over HTTP, or render in the browser, which the rendered tests launch."""
    if request.param:
        request.getfixturevalue("browser_engine")
    return bool(request.param)


def _paths(server: LoopbackServer) -> list[str]:
    return [hop.path for hop in server.received]


async def test_a_redirect_to_a_url_robots_txt_disallows_is_not_fetched(server: LoopbackServer, render: bool) -> None:
    server.route("/robots.txt", ROBOTS_TXT)
    server.route("/a", status=302, headers=[("Location", "/private/page")])
    server.route("/private/page", "<p>private</p>", headers=HTML)
    ignored = Ignored()

    report = await crawl(Pages, settings={**OBEY, "extensions": {ignored: 200}}, urls=[server.url("/a")], render=render)

    assert report.items == []
    assert _paths(server) == ["/robots.txt", "/a"]
    assert ignored.reasons == ["robots.txt"]
    assert (report.stats["robots.disallowed"], report.stats["robots.redirects_disallowed"]) == (1, 1)
    assert (report.stats["requests.ignored"], report.stats["requests.failed"]) == (1, 0)


async def test_a_redirect_to_another_origin_reads_its_robots_txt_once(
    server: LoopbackServer, other_server: LoopbackServer, render: bool
) -> None:
    server.route("/robots.txt", "User-agent: *\n")
    other_server.route("/robots.txt", ROBOTS_TXT)
    for path in ("/a", "/b", "/c"):
        target = "/private" if path == "/c" else path
        server.route(path, status=301, headers=[("Location", other_server.url(target))])
        other_server.route(target, f"<p>{target}</p>", headers=HTML)
    urls = [server.url(path) for path in ("/a", "/b", "/c")]

    report = await crawl(Pages, settings=OBEY, urls=urls, render=render)

    assert _urls(report.items) == [other_server.url("/a"), other_server.url("/b")]
    assert sorted(_paths(other_server)) == ["/a", "/b", "/robots.txt"]
    assert report.stats["robots.fetched"] == 2
    assert report.stats["robots.redirects_disallowed"] == 1


@pytest.mark.parametrize(("settings", "obey"), [({}, True), (OBEY, False)], ids=["off", "exempt"])
async def test_redirects_are_followed_unchecked_without_robots_obey_or_for_a_request_that_does_not_obey(
    server: LoopbackServer, other_server: LoopbackServer, settings: dict[str, Any], obey: bool
) -> None:
    for robots in (server, other_server):
        robots.route("/robots.txt", "User-agent: *\nDisallow: /\n")
    server.route("/a", status=302, headers=[("Location", other_server.url("/private"))])
    other_server.route("/private", "<p>private</p>")

    report = await crawl(Pages, settings=settings, urls=[server.url("/a")], obey=obey)

    assert _urls(report.items) == [other_server.url("/private")]
    assert (_paths(server), _paths(other_server)) == (["/a"], ["/private"])


async def test_a_redirect_to_an_origin_whose_robots_txt_is_unreachable_waits_for_it(
    server: LoopbackServer, other_server: LoopbackServer
) -> None:
    answers = [Reply(status=503), Reply("User-agent: *\n")]

    def robots_txt(_request: ReceivedRequest) -> Reply:
        return answers.pop(0) if len(answers) > 1 else answers[0]

    server.route("/robots.txt", "User-agent: *\n")
    other_server.route("/robots.txt", handler=robots_txt)
    server.route("/a", status=302, headers=[("Location", other_server.url("/b"))])
    other_server.route("/b", "<p>b</p>")

    report = await crawl(Pages, settings=OBEY, urls=[server.url("/a")])

    assert _urls(report.items) == [other_server.url("/b")]
    assert (_paths(server), _paths(other_server)) == (["/robots.txt", "/a", "/a"], ["/robots.txt", "/robots.txt", "/b"])
    assert report.stats["robots.deferred"] == 1
    assert (report.stats["requests.retried"], report.stats["requests.failed"], report.stats["downloads.errors"]) == (
        0,
        0,
        0,
    )
