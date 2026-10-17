"""Runs whose Requests lease Sessions from a Session Pool."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from typing import TYPE_CHECKING, Any, ClassVar

from pernix import Request, Response, Spider, middleware
from pernix.testing import Reply, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.middlewares import Download
    from pernix.testing import LoopbackServer, ReceivedRequest

POOL: Mapping[str, Any] = {"sessions.enabled": True}


def _echo_cookie(request: ReceivedRequest) -> Reply:
    return Reply(body=request.header("Cookie") or "")


def _sign_in(request: ReceivedRequest) -> Reply:
    user = dict(request.query).get("user", "")
    return Reply(body=user, headers=[("Set-Cookie", f"user={user}")])


class Fetch(Spider):
    """Fetches a URL, then the paths it is given in turn, recording the Session and cookies of each."""

    def __init__(self, url: str, then: tuple[str, ...] = (), session: str | None = None) -> None:
        self.url = url
        self.then = list(then)
        self.session = session

    async def start(self) -> AsyncIterator[Request]:
        yield Request(self.url, session=self.session)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"path": response.request.url.rsplit("/", 1)[-1], "session": response.session, "text": response.text}
        if self.then:
            yield Request(self.then.pop(0))


async def test_requests_lease_sessions_by_default_and_keep_their_cookies(server: LoopbackServer) -> None:
    server.route("/sign-in", handler=_sign_in)
    server.route("/echo", handler=_echo_cookie)

    class Leasing(Fetch):
        settings: ClassVar[Mapping[str, Any]] = {**POOL, "sessions.size": 1}

    report = await crawl(Leasing, url=server.url("/sign-in?user=me"), then=("/echo",))

    assert report.items == [
        {"path": "sign-in?user=me", "session": "default-1", "text": "me"},
        {"path": "echo", "session": "default-1", "text": "user=me"},  # the Session kept the cookie
    ]
    assert report.stats["sessions.leased"] == 2
    assert report.stats["sessions.created"] == 1


async def test_a_request_from_a_callback_leases_its_own_session(server: LoopbackServer) -> None:
    server.route("/sign-in", handler=_sign_in)
    server.route("/echo", handler=_echo_cookie)

    class Eager(Fetch):
        settings: ClassVar[Mapping[str, Any]] = {**POOL, "sessions.size": 2, "sessions.eager": True}

    report = await crawl(Eager, url=server.url("/sign-in?user=me"), then=("/echo",), session="default-1")

    assert report.items == [
        {"path": "sign-in?user=me", "session": "default-1", "text": "me"},
        {"path": "echo", "session": "default-2", "text": ""},  # not the Session of the page it was found on
    ]


async def test_named_sessions_are_continued_with_the_session_of_a_response(server: LoopbackServer) -> None:
    server.route("/sign-in", handler=_sign_in)
    server.route("/echo", handler=_echo_cookie)

    class Accounts(Spider):
        settings: ClassVar[Mapping[str, Any]] = POOL

        def __init__(self, base: str) -> None:
            self.base = base

        async def start(self) -> AsyncIterator[Request]:
            for user in ("alice", "bob"):
                yield Request(f"{self.base}/sign-in?user={user}", session=user)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            inline = await self.fetch("/echo")  # an Inline Fetch continues the Session too
            yield Request(
                f"/echo?as={response.session}",  # the Session takes no part in the Fingerprint
                session=response.session,
                callback=self.parse_echo,
                kwargs={"inline": inline.text},
            )

        async def parse_echo(self, response: Response, inline: str) -> AsyncIterator[Any]:
            yield {"session": response.session, "cookie": response.text, "inline": inline}

    report = await crawl(Accounts, base=server.url("").rstrip("/"))

    assert sorted(report.items, key=lambda item: item["session"]) == [
        {"session": "alice", "cookie": "user=alice", "inline": "user=alice"},
        {"session": "bob", "cookie": "user=bob", "inline": "user=bob"},
    ]


async def test_a_429_cools_down_only_its_session_while_others_fetch_from_the_domain(server: LoopbackServer) -> None:
    def limited(request: ReceivedRequest) -> Reply:
        if "user=a" in (request.header("Cookie") or ""):
            return Reply(status=429, headers=[("Retry-After", "60")])
        return Reply(body="served")

    server.route("/sign-in", handler=_sign_in)
    server.route("/limited", handler=limited)

    class Limited(Spider):
        settings: ClassVar[Mapping[str, Any]] = {
            **POOL,
            "retry.times": 0,
            "sessions.cooldown_max": 60,
            "domain.cooldown_max": 60,  # a domain-wide cooldown would hold the other Session for a minute
        }

        def __init__(self, base: str) -> None:
            self.base = base

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/sign-in?user=a", session="a", callback=self.signed_in)

        async def signed_in(self, response: Response) -> AsyncIterator[Any]:
            yield Request("/limited", session="a", errback=self.limited)

        async def limited(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            for number in range(3):
                yield Request(f"/limited?n={number}", session="b", callback=self.served)

        async def served(self, response: Response) -> AsyncIterator[Any]:
            yield {"session": response.session, "text": response.text}

    report = await asyncio.wait_for(crawl(Limited, base=server.url("").rstrip("/")), timeout=20)

    assert report.items == [{"session": "b", "text": "served"}] * 3
    assert report.stats["sessions.cooldowns"] == 1
    assert report.stats["sessions.outcomes.rate_limited"] == 1
    assert report.stats["throttle.cooldowns"] == 0


async def test_a_sessions_max_concurrency_holds_under_concurrency(server: LoopbackServer) -> None:
    in_flight: dict[str | None, int] = defaultdict(int)
    most: dict[str | None, int] = defaultdict(int)

    @middleware
    async def count(request: Request, call_next: Download) -> Response:
        in_flight[request.session] += 1
        most[request.session] = max(most[request.session], in_flight[request.session])
        try:
            await asyncio.sleep(0.02)
            return await call_next(request)
        finally:
            in_flight[request.session] -= 1

    server.route("/page", "page")

    class Busy(Spider):
        settings: ClassVar[Mapping[str, Any]] = {
            **POOL,
            "concurrency": 8,
            "sessions.size": 2,
            "middlewares": {count: 100},
        }

        def __init__(self, url: str) -> None:
            self.url = url

        async def start(self) -> AsyncIterator[Request]:
            for number in range(10):
                yield Request(f"{self.url}?n={number}")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"session": response.session}

    report = await crawl(Busy, url=server.url("/page"))

    assert len(report.items) == 10
    assert set(most) == {"default-1", "default-2"}
    assert max(most.values()) == 1


async def test_without_a_session_pool_requests_stay_independent(server: LoopbackServer) -> None:
    server.route("/sign-in", handler=_sign_in)
    server.route("/echo", handler=_echo_cookie)
    failures: list[Exception] = []

    class Independent(Fetch):
        async def start(self) -> AsyncIterator[Request]:
            async for request in super().start():
                yield request
            yield Request(f"{self.url}&again=1", session="alice", errback=self.failed)

        async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            failures.append(error)
            yield None

    report = await crawl(Independent, url=server.url("/sign-in?user=me"), then=("/echo",))

    assert report.items == [
        {"path": "sign-in?user=me", "session": None, "text": "me"},
        {"path": "echo", "session": None, "text": ""},
    ]
    assert [str(error) for error in failures] == [
        f"<Request GET {server.url('/sign-in?user=me&again=1')}> names the Session 'alice', but no Session Pool is "
        "enabled; enable one with sessions.enabled"
    ]
    assert not any(key.startswith("sessions.") for key in report.stats)
