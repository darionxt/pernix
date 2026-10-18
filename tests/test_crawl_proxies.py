"""Runs whose Requests go through proxies, loopback servers standing in for them."""

from __future__ import annotations

import asyncio
import io
import sys
from typing import TYPE_CHECKING, Any, ClassVar, Self
from urllib.parse import urlsplit

import pytest

from pernix import Request, Response, Spider
from pernix.crawler import Crawler
from pernix.proxies import Proxy as ProxyRecord
from pernix.testing import LoopbackServer, Reply, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from pernix.proxies import Binding
    from pernix.testing import ReceivedRequest

HTML = [("Content-Type", "text/html")]
SHOP = "http://shop.example"
PATHS = tuple(f"/page/{number}" for number in range(8))
"""The pages of the shop, which every proxy answers with its own name."""


class Proxy:
    """A loopback server standing in for a proxy: it answers every page of the shop with its own name.

    It refuses the next `refusals` requests with a 407, as a proxy that fails.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self.refusals = 0
        self.server = LoopbackServer()
        for path in PATHS:
            self.server.route(path, handler=self._answer)

    def _answer(self, _request: ReceivedRequest) -> Reply:
        if self.refusals:
            self.refusals -= 1
            return Reply(status=407)
        return Reply(f"<p>{self.name}</p>", headers=HTML)

    def __enter__(self) -> Self:
        self.server.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.stop()

    @property
    def url(self) -> str:
        return self.server.base_url

    def with_credentials(self, user: str, password: str) -> str:
        return self.url.replace("://", f"://{user}:{password}@", 1)


class Shop(Spider):
    """Fetches the pages of the shop one after another, keeping which proxy answered each."""

    settings: ClassVar[Mapping[str, Any]] = {"concurrency": 1}

    def __init__(self, pages: int = 4, sessions: tuple[str, ...] = ()) -> None:
        self.pages = pages
        self.sessions = sessions

    async def start(self) -> AsyncIterator[Request]:
        for number in range(self.pages):
            session = self.sessions[number % len(self.sessions)] if self.sessions else None
            yield Request(f"{SHOP}{PATHS[number]}", session=session, priority=-number)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"via": response.css("p::text").get(), "session": response.session}


def _via(report_items: list[Any]) -> list[str]:
    return [item["via"] for item in report_items]


async def test_independent_requests_take_the_proxies_in_turn() -> None:
    with Proxy("a") as a, Proxy("b") as b:
        report = await crawl(Shop, settings={"proxies.urls": [a.url, b.url]})

    assert _via(report.items) == ["a", "b", "a", "b"]
    assert (report.stats["proxies.available"], report.stats["proxies.cooldown"]) == (2, 0)


class Provider:
    """A provider of one's own, giving the proxies it was made with."""

    def __init__(self, *proxies: str) -> None:
        self.given = proxies

    async def proxies(self) -> tuple[str, ...]:
        return self.given


@pytest.mark.parametrize("source", ["urls", "file", "api", "provider"])
async def test_each_source_gives_proxies(source: str, server: LoopbackServer, tmp_path: Path) -> None:
    with Proxy("listed") as proxy:
        settings: dict[str, Any]
        match source:
            case "urls":
                settings = {"proxies.urls": [proxy.url]}
            case "file":
                file = tmp_path / "proxies.txt"
                file.write_text(f"# ours\n{proxy.url}\n")
                settings = {"proxies.file": str(file)}
            case "api":
                server.route("/proxies", f"{proxy.url}\r\n")
                settings = {"proxies.api": server.url("/proxies?key=k")}
            case _:
                settings = {"proxies.provider": Provider(proxy.url)}
        report = await crawl(Shop, pages=2, settings=settings)

    assert _via(report.items) == ["listed", "listed"]


async def test_each_session_keeps_its_own_proxy() -> None:
    with Proxy("a") as a, Proxy("b") as b:
        report = await crawl(
            Shop,
            pages=6,
            sessions=("alice", "bob"),
            settings={"sessions.enabled": True, "proxies.urls": [a.url, b.url]},
        )

    via = {item["session"]: set() for item in report.items}
    for item in report.items:
        via[item["session"]].add(item["via"])
    assert sorted(map(sorted, via.values())) == [["a"], ["b"]]


@pytest.mark.parametrize("sessions", [False, True], ids=["independent", "under a Session"])
async def test_a_failing_proxy_is_taken_out_of_use_and_its_request_retried_through_another(
    refused_proxy: str, *, sessions: bool
) -> None:
    with Proxy("working") as working:
        report = await crawl(
            Shop,
            pages=3,
            sessions=("alice",) if sessions else (),
            settings={"sessions.enabled": sessions, "proxies.urls": [refused_proxy, working.url]},
        )

    assert _via(report.items) == ["working"] * 3
    assert report.stats["requests.retried"] == 1
    assert (report.stats["proxies.failures"], report.stats["proxies.cooldowns"]) == (1, 1)
    assert report.stats["proxies.cooldown"] == 1


@pytest.mark.usefixtures("browser_engine")
async def test_the_rendered_and_http_requests_of_a_session_go_through_its_proxy() -> None:
    class Both(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"concurrency": 1, "sessions.enabled": True}

        async def start(self) -> AsyncIterator[Request]:
            for number, render in enumerate((False, True, False, True)):
                session = ("alice", "bob")[number // 2]
                yield Request(f"{SHOP}{PATHS[number]}", render=render, session=session, priority=-number)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"session": response.session, "fetcher": response.fetcher, "via": response.css("p::text").get()}

    with Proxy("a") as a, Proxy("b") as b:
        report = await crawl(Both, settings={"proxies.urls": [a.url, b.url]})

    by_session: dict[str, set[str]] = {}
    for item in report.items:
        by_session.setdefault(item["session"], set()).add(item["via"])
    fetched = sorted((item["session"], item["fetcher"]) for item in report.items)  # the browser has workers of its own
    assert fetched == [("alice", "browser"), ("alice", "http"), ("bob", "browser"), ("bob", "http")]
    assert sorted(map(sorted, by_session.values())) == [["a"], ["b"]]


async def test_proxy_credentials_never_appear_in_the_logs_or_stats(
    refused_proxy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    class Careless(Shop):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            self.logger.info("fetched through %s", response.request.proxy)
            async for item in super().parse(response):
                yield item

    with Proxy("working") as working:
        crawler = Crawler.from_spider(
            Careless,
            args={"pages": 2},
            settings={
                "proxies.urls": [
                    refused_proxy.replace("://", "://refused-user:refused-pw@", 1),
                    working.with_credentials("working-user", "working-pw"),
                ],
                "retry.delay_scale": 0,
                "stats.interval": 0,
                "log.output": "stderr",
                "log.format": "json",
                "log.level": "DEBUG",
            },
        )
        async with asyncio.timeout(20):
            await crawler.crawl()

    output = stream.getvalue()
    assert "fetched through http://***@127.0.0.1" in output
    assert crawler.stats.get("proxies.failures") == 1
    for secret in ("refused-user", "refused-pw", "working-user", "working-pw"):
        assert secret not in output
        assert all(secret not in f"{key}={value}" for key, value in crawler.stats.snapshot().items())


async def test_once_no_proxy_can_be_used_the_run_drains_keeping_its_requests(
    refused_proxy: str, caplog: pytest.LogCaptureFixture
) -> None:
    report = await asyncio.wait_for(
        crawl(Shop, pages=3, settings={"proxies.urls": [refused_proxy], "proxies.max_failures": 1}), timeout=20
    )

    assert report.reason == "drained"
    assert report.items == []
    assert report.stats["requests.failed"] == 0
    assert report.stats["proxies.retired"] == 1
    assert "No proxy can be used: every proxy is retired" in caplog.text


async def test_the_proxies_of_an_api_are_refreshed(server: LoopbackServer) -> None:
    with Proxy("old") as old, Proxy("new") as new:
        listed = {"proxies": old.url}

        def answer(_request: ReceivedRequest) -> Reply:
            return Reply(listed["proxies"])

        server.route("/proxies", handler=answer)

        class Later(Shop):
            async def parse(self, response: Response) -> AsyncIterator[Any]:
                async for item in super().parse(response):
                    yield item
                if response.url.endswith(PATHS[0]):
                    listed["proxies"] = new.url
                    await asyncio.sleep(0.3)  # a refresh or more meanwhile
                    yield Request(f"{SHOP}{PATHS[1]}")

        report = await crawl(Later, pages=1, settings={"proxies.api": server.url("/proxies"), "proxies.refresh": 0.05})

    assert _via(report.items) == ["old", "new"]
    refreshes = report.stats["proxies.refreshes"]
    assert isinstance(refreshes, int)
    assert refreshes >= 1


class ByRegion:
    """Binds each Session to a proxy of its region, which it keeps while it is available."""

    REGIONS: ClassVar[Mapping[str, str]] = {"alice": "eu", "bob": "us"}

    async def bind(self, binding: Binding) -> str | None:
        if binding.reason == "lease":
            return binding.current
        region = self.REGIONS[binding.session]
        return next((candidate.url for candidate in binding.open if region in candidate.tags), None)


async def test_a_binding_policy_of_ones_own_binds_sessions_by_tag_and_keeps_them_there() -> None:
    with Proxy("eu") as eu, Proxy("us") as us, Proxy("untagged") as untagged:
        report = await crawl(
            Shop,
            pages=6,
            sessions=("alice", "bob"),
            settings={
                "sessions.enabled": True,
                "proxies.urls": [
                    untagged.url,
                    {"url": eu.url, "tags": ["eu"]},
                    {"url": us.url, "tags": ["us"]},
                ],
                "proxies.binding": ByRegion,
            },
        )

    assert {(item["session"], item["via"]) for item in report.items} == {("alice", "eu"), ("bob", "us")}
    assert len(report.items) == 6


async def test_with_max_sessions_1_no_two_sessions_share_a_proxy() -> None:
    with Proxy("a") as a, Proxy("b") as b:
        report = await asyncio.wait_for(
            crawl(
                Shop,
                pages=6,
                settings={
                    "concurrency": 3,
                    "sessions.enabled": True,
                    "sessions.eager": True,
                    "sessions.size": 3,
                    "proxies.urls": [a.url, b.url],
                    "proxies.max_sessions": 1,
                },
            ),
            timeout=20,
        )

    sessions: dict[str, set[str]] = {}
    for item in report.items:
        sessions.setdefault(item["via"], set()).add(item["session"])
    assert len(report.items) == 6
    assert all(len(each) == 1 for each in sessions.values())
    waits = report.stats["proxies.binding_waits"]
    assert isinstance(waits, int)
    assert waits >= 1


class OneAddress(Spider):
    """Fetches the pages of the shop one after another under one Account, which its site allows one address."""

    settings: ClassVar[Mapping[str, Any]] = {
        "concurrency": 1,
        "sessions.enabled": True,
        "proxies.max_sessions": 1,
        "proxies.cooldown": 0.1,
    }

    def __init__(self, pages: int = 4) -> None:
        self.pages = pages

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{SHOP}{PATHS[0]}", session="alice")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"via": response.css("p::text").get(), "session": response.session}
        number = PATHS.index(urlsplit(response.url).path)
        self.parsed(number)
        if number + 1 < self.pages:
            yield Request(f"{SHOP}{PATHS[number + 1]}", session="alice")

    def parsed(self, number: int) -> None:
        """Called once each page is parsed."""


async def test_with_retired_a_session_waits_for_its_proxy_and_changes_it_only_once_it_is_retired() -> None:
    with Proxy("a") as a, Proxy("b") as b:

        class Failing(OneAddress):
            def parsed(self, number: int) -> None:
                a.refusals = (1, 3)[number] if number < 2 else 0  # one cooldown, then a row that Retires it

        report = await asyncio.wait_for(
            crawl(
                Failing,
                settings={
                    "proxies.urls": [a.url, b.url],
                    "proxies.rebind": "retired",
                    "proxies.max_failures": 3,
                    "retry.times": 5,
                },
            ),
            timeout=20,
        )

    assert _via(report.items) == ["a", "a", "b", "b"]
    assert len(b.server.received) == 2  # never through b while it waited for a
    assert report.stats["sessions.outcomes.proxy_failed"] == 4
    assert report.stats["proxies.retired"] == 1


async def test_with_never_a_session_is_retired_with_its_proxy_and_the_run_goes_on_with_the_others() -> None:
    with Proxy("a") as a, Proxy("b") as b:

        class Failing(Shop):
            async def parse(self, response: Response) -> AsyncIterator[Any]:
                async for item in super().parse(response):
                    yield item
                if response.url.endswith(PATHS[1]):
                    a.refusals = 1

        report = await asyncio.wait_for(
            crawl(
                Failing,
                pages=6,
                settings={
                    "sessions.enabled": True,
                    "sessions.size": 2,
                    "sessions.eager": True,
                    "proxies.urls": [a.url, b.url],
                    "proxies.max_sessions": 1,
                    "proxies.max_failures": 1,
                    "proxies.rebind": "never",
                },
            ),
            timeout=20,
        )

    first, second = report.items[0]["session"], report.items[1]["session"]
    assert [(item["session"], item["via"]) for item in report.items] == [(first, "a"), *[(second, "b")] * 5]
    assert report.stats["sessions.retirements"] == 1
    assert report.stats["sessions.outcomes.proxy_retired"] == 1
    assert report.stats.get("requests.failed", 0) == 0


ACCOUNTS = [{"username": "alice", "password": "account-secret"}, {"username": "bob", "password": "account-secret"}]


def _own_address(own: Mapping[str, str]) -> type:
    """A binding policy giving each Account the proxy of its own `own` names."""

    class OwnAddress:
        async def bind(self, binding: Binding) -> ProxyRecord | None:
            return None if binding.account is None else ProxyRecord(own[binding.account.id])

    return OwnAddress


@pytest.mark.usefixtures("browser_engine")
async def test_a_policy_giving_each_account_its_own_proxy_sends_its_requests_http_and_rendered_through_it() -> None:
    class Accounts(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"concurrency": 1, "sessions.enabled": True}

        async def start(self) -> AsyncIterator[Request]:
            for number, (account, render) in enumerate(
                [("alice", False), ("alice", True), ("bob", False), ("bob", True)]
            ):
                yield Request(f"{SHOP}{PATHS[number]}", render=render, session=account, priority=-number)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"session": response.session, "fetcher": response.fetcher, "via": response.css("p::text").get()}

    with Proxy("alice's") as alice, Proxy("bob's") as bob:
        own = {"alice": alice.with_credentials("alice", "alice-pw"), "bob": bob.with_credentials("bob", "bob-pw")}
        report = await crawl(Accounts, settings={"sessions.accounts": ACCOUNTS, "proxies.binding": _own_address(own)})

    assert sorted((item["session"], item["fetcher"], item["via"]) for item in report.items) == [
        ("alice", "browser", "alice's"),
        ("alice", "http", "alice's"),
        ("bob", "browser", "bob's"),
        ("bob", "http", "bob's"),
    ]


async def test_the_credentials_of_proxies_of_their_own_never_appear_in_the_logs_or_stats(
    refused_proxy: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)

    with Proxy("alice's") as alice:
        own = {
            "alice": alice.with_credentials("alice-user", "alice-pw"),
            "bob": refused_proxy.replace("://", "://bob-user:bob-pw@", 1),
        }

        class Careless(Shop):
            async def parse(self, response: Response) -> AsyncIterator[Any]:
                assert response.session is not None
                self.logger.info("fetched through %s", own[response.session])
                async for item in super().parse(response):
                    yield item

        crawler = Crawler.from_spider(
            Careless,
            args={"pages": 4, "sessions": ("alice", "bob")},
            settings={
                "sessions.enabled": True,
                "sessions.accounts": ACCOUNTS,
                "proxies.binding": _own_address(own),
                "proxies.max_failures": 1,
                "retry.delay_scale": 0,
                "stats.interval": 0,
                "log.output": "stderr",
                "log.format": "json",
                "log.level": "DEBUG",
            },
        )
        async with asyncio.timeout(20):
            await crawler.crawl()

    output = stream.getvalue()
    assert "fetched through http://***@127.0.0.1" in output
    assert crawler.stats.get("sessions.retirements") == 1  # bob, with the proxy that failed
    for secret in ("alice-user", "alice-pw", "bob-user", "bob-pw"):
        assert secret not in output
        assert all(secret not in f"{key}={value}" for key, value in crawler.stats.snapshot().items())
