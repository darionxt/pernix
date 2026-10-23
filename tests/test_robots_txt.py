"""The robots.txt layer of a Run's download chain, against a fake rest of the chain and a Throttle on a fake clock."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlsplit

import pytest

from pernix import IgnoreRequest, Request, Response
from pernix.errors import ConnectError, NetworkBlocked, ProxyError, Timeout, TooManyRedirects
from pernix.fetchers import FetcherDispatch, StreamedResponse
from pernix.fetchers.redirects import redirect_check
from pernix.frontiers import host_slot
from pernix.headers import Headers
from pernix.response import Redirect
from pernix.robots import CACHE_TTL, PARSE_LIMIT, UNREACHABLE_WAIT, RobotsTxt, is_robots_txt
from pernix.sending import NotYet
from pernix.settings import DomainSettings, RobotsSettings
from pernix.stats import Stats
from pernix.testing.frontier import FakeClock
from pernix.throttle import Throttle
from pernix.urls import url_origin

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Callable, Mapping

    from pernix.middlewares import Download

SITE = "https://site.example"
HOST = "site.example"
OTHER = "https://other.example"
OTHER_HOST = "other.example"
ROBOTS_TXT = b"User-agent: *\nDisallow: /private\n"

type Answer = int | bytes | Response | Exception


def _response(
    request: Request, status: int, body: bytes = b"", headers: list[tuple[str, str]] | None = None
) -> Response:
    return Response(url=request.url, status=status, headers=headers or [], body=body, request=request)


class Site:
    """The rest of a download chain: answers robots.txt with its answers in turn, the last repeating, and pages with 200."""

    def __init__(self, *answers: Answer) -> None:
        self.answers = list(answers) or [ROBOTS_TXT]
        self.requests: list[Request] = []
        self.release: asyncio.Event | None = None

    @property
    def robots(self) -> list[Request]:
        return [request for request in self.requests if is_robots_txt(request.url)]

    @property
    def pages(self) -> list[str]:
        return [request.url for request in self.requests if not is_robots_txt(request.url)]

    async def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        if not is_robots_txt(request.url):
            return _response(request, 200, b"page")
        if self.release is not None:
            await self.release.wait()
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        match answer:
            case Exception():
                raise answer
            case Response():
                return answer.replace(request=request)
            case int():
                return _response(request, answer)
            case bytes():
                return _response(request, 200, answer)


class Redirecting:
    """The rest of a download chain to several origins, a Site each, following redirects as a Fetcher does.

    Each redirect is checked with the check in effect, if any, before it is
    followed. Every URL reached is recorded, those that redirect included.
    """

    def __init__(self, sites: Mapping[str, Site], redirects: Mapping[str, str]) -> None:
        self.sites = sites
        self.redirects = redirects
        self.reached: list[str] = []
        self.checked: list[str] = []

    async def __call__(self, request: Request) -> Response:
        url = request.url
        self.reached.append(url)
        while (target := self.redirects.get(url)) is not None:
            check = redirect_check()
            if check is not None:
                self.checked.append(target)
                await check(target)
            url = target
            self.reached.append(url)
        site = self.sites[url_origin(url) or ""]
        return await site(request.replace(url=url))


class Obeying:
    """A robots.txt layer, on a Throttle and a clock of its own."""

    def __init__(
        self,
        *,
        cooldown_max: float = 300.0,
        delay: float = 0.0,
        slot_of: Callable[[Request], str] = host_slot,
        **settings: object,
    ) -> None:
        self.clock = FakeClock()
        self.throttle = Throttle(
            DomainSettings(cooldown_max=cooldown_max, delay=delay, delay_jitter=0.0), clock=self.clock
        )
        self.stats = Stats()
        self.layer = RobotsTxt(
            RobotsSettings(obey=True, **settings),  # pyright: ignore[reportArgumentType]
            throttle=self.throttle,
            stats=self.stats,
            cooldown_max=cooldown_max,
            slot_of=slot_of,
            clock=self.clock,
        )

    async def fetch(self, url: str, site: Download, **options: object) -> Response:
        return await self.layer(Request(url, **options), site)  # pyright: ignore[reportArgumentType]

    def stat(self, key: str) -> object:
        return self.stats.snapshot().get(key)


async def test_the_first_fetch_to_an_origin_fetches_its_robots_txt_as_part_of_itself() -> None:
    site = Site()
    obeying = Obeying()

    response = await obeying.fetch(
        f"{SITE}/page", site, session="s1", proxy="http://proxy.example:8080", impersonate="chrome150"
    )
    await obeying.fetch(f"{SITE}/other", site)

    assert response.body == b"page"
    [robots] = site.robots
    assert (robots.url, robots.method, robots.fetcher) == (f"{SITE}/robots.txt", "GET", "http")
    assert (robots.session, robots.proxy, robots.impersonate) == ("s1", "http://proxy.example:8080", "chrome150")
    assert robots.headers.get("Referer") == ""  # typed in, from no page
    assert site.pages == [f"{SITE}/page", f"{SITE}/other"]
    assert (obeying.stat("robots.fetched"), obeying.stat("robots.by_status.200")) == (1, 1)


async def test_each_origin_has_robots_txt_of_its_own() -> None:
    site = Site()
    obeying = Obeying()

    for url in (f"{SITE}/a", f"http://{HOST}/a", f"{SITE}:8443/a", "https://other.example/a", f"{SITE}/b"):
        await obeying.fetch(url, site)

    assert [robots.url for robots in site.robots] == [
        f"{SITE}/robots.txt",
        f"http://{HOST}/robots.txt",
        f"{SITE}:8443/robots.txt",
        "https://other.example/robots.txt",
    ]


async def test_a_disallowed_request_is_ignored_without_being_fetched() -> None:
    site = Site()
    obeying = Obeying()

    with pytest.raises(IgnoreRequest) as raised:
        await obeying.fetch(f"{SITE}/private/a", site)

    assert raised.value.reason == "robots.txt"
    assert site.pages == []
    assert obeying.stat("robots.disallowed") == 1


async def test_the_product_token_picks_the_group() -> None:
    site = Site(b"User-agent: *\nDisallow: /\n\nUser-agent: ExampleBot\nAllow: /\n")

    with pytest.raises(IgnoreRequest):
        await Obeying().fetch(f"{SITE}/a", site)
    assert (await Obeying(user_agent="ExampleBot").fetch(f"{SITE}/a", site)).body == b"page"


async def test_concurrent_fetches_to_an_origin_wait_for_one_robots_txt() -> None:
    site = Site()
    site.release = asyncio.Event()
    obeying = Obeying()

    fetches = [asyncio.create_task(obeying.fetch(f"{SITE}/{n}", site)) for n in range(3)]
    for _ in range(10):
        await asyncio.sleep(0)
    assert (len(site.robots), site.pages) == (1, [])
    assert not any(fetch.done() for fetch in fetches)
    site.release.set()
    async with asyncio.timeout(5):
        await asyncio.gather(*fetches)

    assert len(site.robots) == 1
    assert sorted(site.pages) == [f"{SITE}/{n}" for n in range(3)]


@pytest.mark.parametrize("error", [ProxyError("proxy down"), NetworkBlocked("refused"), ValueError("a bug")])
async def test_errors_that_tell_of_the_requests_way_are_its_own_and_the_next_fetch_tries_again(
    error: Exception,
) -> None:
    site = Site(error, ROBOTS_TXT)
    site.release = asyncio.Event()
    obeying = Obeying()

    first = asyncio.create_task(obeying.fetch(f"{SITE}/a", site))
    second = asyncio.create_task(obeying.fetch(f"{SITE}/b", site))
    for _ in range(10):
        await asyncio.sleep(0)
    site.release.set()
    async with asyncio.timeout(5):
        with pytest.raises(type(error)):
            await first
        await second

    assert (len(site.robots), site.pages) == (2, [f"{SITE}/b"])
    assert obeying.stat(f"robots.errors_by_type.{type(error).__name__}") == 1
    assert obeying.throttle.cooldown_left(HOST) == 0


def _redirected(times: int) -> Response:
    request = Request(f"{SITE}/robots.txt")
    history = [Redirect(f"{SITE}/r{n}", 302, Headers()) for n in range(times)]
    return Response(
        url=f"{SITE}/r{times}",
        status=200,
        headers=[],
        body=b"User-agent: *\nDisallow: /",
        request=request,
        history=history,
    )


@pytest.mark.parametrize(
    "answer", [404, 401, 403, 304, TooManyRedirects("redirected more than 5 times"), _redirected(6)]
)
async def test_without_a_robots_txt_for_the_crawler_everything_is_allowed(answer: Answer) -> None:
    site = Site(answer)
    obeying = Obeying()

    assert (await obeying.fetch(f"{SITE}/private", site)).body == b"page"
    await obeying.fetch(f"{SITE}/a", site)

    assert len(site.robots) == 1


async def test_a_robots_txt_reached_within_five_redirects_applies() -> None:
    with pytest.raises(IgnoreRequest):
        await Obeying().fetch(f"{SITE}/a", Site(_redirected(5)))


@pytest.mark.parametrize("answer", [503, 500, 429, Timeout("slow"), ConnectError("refused")])
async def test_an_unreachable_robots_txt_holds_requests_back_then_disallows_the_origin(answer: Answer) -> None:
    site = Site(answer)
    obeying = Obeying()

    for fetched in (1, 2):
        with pytest.raises(NotYet) as raised:
            await obeying.fetch(f"{SITE}/a", site)
        assert (raised.value.delay, raised.value.reason) == (UNREACHABLE_WAIT, "robots.txt unreachable")
        assert obeying.throttle.cooldown_left(HOST) == UNREACHABLE_WAIT
        obeying.clock.advance(UNREACHABLE_WAIT - 1)
        with pytest.raises(NotYet) as raised:
            await obeying.fetch(f"{SITE}/b", site)
        assert raised.value.delay == 1
        assert len(site.robots) == fetched
        obeying.clock.advance(1)

    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/a", site)
    obeying.clock.advance(CACHE_TTL - 1)
    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/b", site)

    assert len(site.robots) == 3
    assert site.pages == []
    assert (obeying.stat("robots.fetched"), obeying.stat("robots.deferred"), obeying.stat("robots.disallowed")) == (
        3,
        4,
        2,
    )


@pytest.mark.parametrize(("retry_after", "wait"), [("120", 120.0), ("900", 300.0)])
async def test_a_retry_after_sets_how_long_requests_wait_for_robots_txt(retry_after: str, wait: float) -> None:
    request = Request(f"{SITE}/robots.txt")
    site = Site(_response(request, 503, headers=[("Retry-After", retry_after)]))
    obeying = Obeying(cooldown_max=300.0)

    with pytest.raises(NotYet) as raised:
        await obeying.fetch(f"{SITE}/a", site)

    assert raised.value.delay == wait
    assert obeying.throttle.cooldown_left(HOST) == wait


async def test_rules_fetched_before_stay_in_force_while_robots_txt_is_unreachable() -> None:
    site = Site(ROBOTS_TXT, 503, b"")
    obeying = Obeying()
    await obeying.fetch(f"{SITE}/a", site)
    obeying.clock.advance(CACHE_TTL)

    await obeying.fetch(f"{SITE}/b", site)
    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/private", site)
    obeying.clock.advance(UNREACHABLE_WAIT - 1)
    await obeying.fetch(f"{SITE}/c", site)
    assert len(site.robots) == 2
    obeying.clock.advance(1)
    await obeying.fetch(f"{SITE}/private", site)

    assert len(site.robots) == 3
    assert site.pages == [f"{SITE}/a", f"{SITE}/b", f"{SITE}/c", f"{SITE}/private"]
    assert obeying.throttle.cooldown_left(HOST) == 0


async def test_rules_are_fetched_again_after_a_day() -> None:
    site = Site(b"User-agent: *\nDisallow: /a\n", b"User-agent: *\nDisallow: /b\n")
    obeying = Obeying()
    await obeying.fetch(f"{SITE}/b", site)
    obeying.clock.advance(CACHE_TTL - 1)
    await obeying.fetch(f"{SITE}/b", site)
    obeying.clock.advance(1)

    await obeying.fetch(f"{SITE}/a", site)
    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/b", site)

    assert len(site.robots) == 2


@pytest.mark.parametrize(("crawl_delay_max", "spacing"), [(None, 4.0), (2.5, 2.5)])
async def test_a_crawl_delay_longer_than_the_delay_spaces_the_domain(
    crawl_delay_max: float | None, spacing: float
) -> None:
    site = Site(b"User-agent: *\nCrawl-delay: 4\n")
    obeying = Obeying(delay=1.0, crawl_delay_max=crawl_delay_max)

    await obeying.fetch(f"{SITE}/a", site)

    obeying.clock.advance(spacing - 0.01)
    assert obeying.throttle.blocked() == {HOST}
    obeying.clock.advance(0.01)
    assert obeying.throttle.blocked() == frozenset()


async def test_the_domain_takes_the_longest_crawl_delay_of_its_origins() -> None:
    site = Site(b"User-agent: *\nCrawl-delay: 3\n", b"User-agent: *\nCrawl-delay: 6\n", b"User-agent: *\n")
    obeying = Obeying()
    await obeying.fetch(f"{SITE}/a", site)
    obeying.clock.advance(6.0)
    await obeying.fetch(f"http://{HOST}/a", site)

    obeying.clock.advance(5.99)
    assert obeying.throttle.blocked() == {HOST}
    obeying.clock.advance(CACHE_TTL)
    await obeying.fetch(f"http://{HOST}/a", site)  # its robots.txt no longer asks for one
    obeying.clock.advance(2.99)
    assert obeying.throttle.blocked() == {HOST}  # the https origin's still does
    obeying.clock.advance(0.01)
    assert obeying.throttle.blocked() == frozenset()


async def test_the_slot_of_a_request_is_the_one_the_run_computes() -> None:
    obeying = Obeying(slot_of=lambda request: "everything")

    await obeying.fetch(f"{SITE}/a", Site(b"User-agent: *\nCrawl-delay: 4\n"))
    with pytest.raises(NotYet):
        await obeying.fetch("https://other.example/a", Site(503))

    assert obeying.throttle.blocked() == {"everything"}
    assert obeying.throttle.cooldown_left("everything") == UNREACHABLE_WAIT
    assert obeying.throttle.cooldown_left(HOST) == 0


async def test_every_slot_an_origin_is_fetched_in_takes_its_crawl_delay_as_it_is_now() -> None:
    site = Site(b"User-agent: *\nCrawl-delay: 4\n", b"User-agent: *\nCrawl-delay: 1\n")
    obeying = Obeying(slot_of=lambda request: urlsplit(request.url).path.split("/")[1])

    await obeying.fetch(f"{SITE}/a/1", site)
    assert await obeying.throttle.pace("b")  # as a Run paces a fetch before its download chain
    await obeying.fetch(f"{SITE}/b/1", site)  # with the rules kept
    obeying.clock.advance(3.99)
    assert obeying.throttle.blocked() == {"a", "b"}
    obeying.clock.advance(CACHE_TTL)
    await obeying.fetch(f"{SITE}/a/2", site)  # with the rules fetched again
    await obeying.fetch(f"{SITE}/b/2", site)
    assert await obeying.throttle.pace("b")

    obeying.clock.advance(0.99)
    assert obeying.throttle.blocked() == {"a", "b"}
    obeying.clock.advance(0.01)
    assert obeying.throttle.blocked() == frozenset()
    assert len(site.robots) == 2


async def test_a_crawl_delay_no_longer_than_the_delay_changes_nothing() -> None:
    obeying = Obeying(delay=2.0)

    await obeying.fetch(f"{SITE}/a", Site(b"User-agent: *\nCrawl-delay: 1\n"))

    obeying.clock.advance(1.99)
    assert obeying.throttle.blocked() == {HOST}
    obeying.clock.advance(0.01)
    assert obeying.throttle.blocked() == frozenset()


@pytest.mark.parametrize(
    ("url", "options"),
    [
        (f"{SITE}/private", {"ext": {"robots": {"obey": False}}}),
        (f"{SITE}/robots.txt", {}),
        ("s3://bucket/private", {"fetcher": "s3"}),
    ],
)
async def test_some_requests_go_through_without_robots_txt(url: str, options: dict[str, object]) -> None:
    site = Site()

    await Obeying().fetch(url, site, **options)

    assert [request.url for request in site.requests] == [url]


class Streaming:
    """A Fetcher of one origin that streams its robots.txt in chunks, tracking its open transfers."""

    schemes: ClassVar[frozenset[str]] = frozenset({"https"})

    def __init__(self, robots_txt: bytes) -> None:
        self.robots_txt = robots_txt
        self.open = 0
        self.chunks_read = 0

    async def fetch(self, request: Request) -> Response:
        return _response(request, 200, b"page")

    @asynccontextmanager
    async def stream(self, request: Request, *, max_bytes: int | None = None) -> AsyncGenerator[StreamedResponse]:
        self.open += 1
        try:
            yield StreamedResponse(_response(request, 200), self._chunks, max_bytes=max_bytes or None)
        finally:
            self.open -= 1

    async def _chunks(self) -> AsyncIterator[bytes]:
        size = 64 * 1024
        for start in range(0, len(self.robots_txt), size):
            self.chunks_read += 1
            yield self.robots_txt[start : start + size]


async def test_robots_txt_is_read_up_to_the_parse_limit_and_its_transfer_closed() -> None:
    padding = b"#" * (PARSE_LIMIT - len(ROBOTS_TXT) - 10) + b"\n"
    fetcher = Streaming(ROBOTS_TXT + padding + b"Disallow: /cut\n" + b"#" * PARSE_LIMIT + b"\nDisallow: /later\n")
    dispatch = FetcherDispatch({"http": fetcher})
    obeying = Obeying()

    async def site(request: Request) -> Response:
        return await dispatch(request)

    with pytest.raises(IgnoreRequest):
        await obeying.layer(Request(f"{SITE}/private"), site)
    for path in ("/cut", "/later"):  # the line the limit cuts is left out
        assert (await obeying.layer(Request(f"{SITE}{path}"), site)).body == b"page"

    assert fetcher.open == 0
    assert fetcher.chunks_read == PARSE_LIMIT // (64 * 1024) + 1
    assert obeying.stat("downloads.bytes") == fetcher.chunks_read * 64 * 1024


def _two_origins(other: Site, *redirects: tuple[str, str]) -> Redirecting:
    """SITE, with its robots.txt, and OTHER, as `other` answers, with the redirects given."""
    return Redirecting({SITE: Site(), OTHER: other}, dict(redirects))


async def test_a_redirect_to_a_url_its_origin_disallows_is_not_followed() -> None:
    chain = Redirecting({SITE: Site()}, {f"{SITE}/a": f"{SITE}/private/a"})
    obeying = Obeying()

    with pytest.raises(IgnoreRequest) as raised:
        await obeying.fetch(f"{SITE}/a", chain)

    assert raised.value.reason == "robots.txt"
    assert chain.reached == [f"{SITE}/robots.txt", f"{SITE}/a"]
    assert (obeying.stat("robots.disallowed"), obeying.stat("robots.redirects_disallowed")) == (1, 1)
    assert obeying.stat("robots.fetched") == 1


async def test_a_redirect_to_another_origin_reads_its_robots_txt_once_as_the_request_is_sent() -> None:
    other = Site(b"User-agent: *\nDisallow: /secret\n")
    chain = _two_origins(
        other, (f"{SITE}/a", f"{OTHER}/a"), (f"{SITE}/b", f"{OTHER}/b"), (f"{SITE}/c", f"{OTHER}/secret")
    )
    obeying = Obeying()
    options = {"session": "s1", "proxy": "http://proxy.example:8080", "impersonate": "chrome150"}

    first = await obeying.fetch(f"{SITE}/a", chain, **options)
    await obeying.fetch(f"{SITE}/b", chain)
    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/c", chain)

    assert first.url == f"{OTHER}/a"
    [robots] = other.robots
    assert (robots.url, robots.fetcher, robots.headers.get("Referer")) == (f"{OTHER}/robots.txt", "http", "")
    assert (robots.session, robots.proxy, robots.impersonate) == ("s1", "http://proxy.example:8080", "chrome150")
    assert other.pages == [f"{OTHER}/a", f"{OTHER}/b"]
    assert (obeying.stat("robots.fetched"), obeying.stat("robots.redirects_disallowed")) == (2, 1)


async def test_each_redirect_of_a_chain_is_checked_and_robots_txt_always_allowed() -> None:
    chain = _two_origins(
        Site(b"User-agent: *\nDisallow: /\n"),
        (f"{SITE}/a", f"{SITE}/b"),
        (f"{SITE}/b", f"{OTHER}/robots.txt"),
    )

    response = await Obeying().fetch(f"{SITE}/a", chain)

    assert response.url == f"{OTHER}/robots.txt"
    assert chain.checked == [f"{SITE}/b", f"{OTHER}/robots.txt"]
    assert chain.reached == [f"{SITE}/robots.txt", f"{SITE}/a", f"{SITE}/b", f"{OTHER}/robots.txt"]


@pytest.mark.parametrize(
    ("url", "options"),
    [(f"{SITE}/a", {"ext": {"robots": {"obey": False}}}), (f"{SITE}/robots.txt", {})],
)
async def test_the_redirects_of_requests_robots_txt_does_not_apply_to_are_not_checked(
    url: str, options: dict[str, object]
) -> None:
    chain = _two_origins(Site(b"User-agent: *\nDisallow: /\n"), (url, f"{OTHER}/private"))

    response = await Obeying().fetch(url, chain, **options)

    assert response.url == f"{OTHER}/private"
    assert chain.checked == []


async def test_the_redirects_of_robots_txt_itself_are_not_checked() -> None:
    other = Site(b"User-agent: *\nDisallow: /\n")
    chain = _two_origins(other, (f"{SITE}/robots.txt", f"{OTHER}/rules.txt"))
    obeying = Obeying()

    await obeying.fetch(f"{SITE}/a", chain)

    assert chain.checked == []
    assert [request.url for request in other.requests] == [f"{OTHER}/rules.txt"]


async def test_a_fetch_that_does_not_obey_never_takes_the_check_of_the_fetch_it_is_nested_in() -> None:
    chain = _two_origins(Site(b"User-agent: *\nDisallow: /\n"), (f"{SITE}/token", f"{OTHER}/token"))
    obeying = Obeying()

    async def authorizing(request: Request) -> Response:
        if request.url == f"{SITE}/a":  # fetches a token on the way, as some Middleware does
            exempt = Request(f"{SITE}/token", ext={"robots": {"obey": False}})
            token = await obeying.layer(exempt, chain)
            return token.replace(request=request)
        return await chain(request)

    response = await obeying.layer(Request(f"{SITE}/a"), authorizing)

    assert response.url == f"{OTHER}/token"
    assert chain.checked == []


async def test_an_unreachable_robots_txt_where_a_redirect_leads_holds_the_request_back_then_disallows() -> None:
    chain = _two_origins(Site(503), (f"{SITE}/a", f"{OTHER}/a"))
    obeying = Obeying()

    for _ in range(2):
        with pytest.raises(NotYet) as raised:
            await obeying.fetch(f"{SITE}/a", chain)
        assert (raised.value.delay, raised.value.reason) == (UNREACHABLE_WAIT, "robots.txt unreachable")
        assert (obeying.throttle.cooldown_left(OTHER_HOST), obeying.throttle.cooldown_left(HOST)) == (
            UNREACHABLE_WAIT,
            0,
        )
        obeying.clock.advance(UNREACHABLE_WAIT)
    with pytest.raises(IgnoreRequest):
        await obeying.fetch(f"{SITE}/a", chain)

    assert f"{OTHER}/a" not in chain.reached
    assert (obeying.stat("robots.deferred"), obeying.stat("robots.redirects_disallowed")) == (2, 1)


@pytest.mark.parametrize("error", [ProxyError("proxy down"), ValueError("a bug")])
async def test_errors_of_the_robots_txt_where_a_redirect_leads_that_tell_of_the_requests_way_are_its_own(
    error: Exception,
) -> None:
    chain = _two_origins(Site(error, b""), (f"{SITE}/a", f"{OTHER}/a"))
    obeying = Obeying()

    with pytest.raises(type(error)):
        await obeying.fetch(f"{SITE}/a", chain)
    await obeying.fetch(f"{SITE}/a", chain)

    assert chain.reached.count(f"{OTHER}/robots.txt") == 2
    assert obeying.throttle.cooldown_left(OTHER_HOST) == 0


async def test_the_crawl_delay_of_the_origin_a_redirect_leads_to_spaces_its_own_slot_alone() -> None:
    chain = _two_origins(Site(b"User-agent: *\nCrawl-delay: 4\n"), (f"{SITE}/a", f"{OTHER}/a"))
    obeying = Obeying()

    await obeying.fetch(f"{SITE}/a", chain)
    assert obeying.throttle.blocked() == frozenset()  # the slot of the Request, which robots.txt took a start of
    assert await obeying.throttle.pace(OTHER_HOST)  # a Request to the other origin starts

    obeying.clock.advance(3.99)
    assert obeying.throttle.blocked() == {OTHER_HOST}
    obeying.clock.advance(0.01)
    assert obeying.throttle.blocked() == frozenset()
