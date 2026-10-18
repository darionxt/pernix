"""Runs whose Requests go through proxies, loopback servers standing in for them."""

from __future__ import annotations

import asyncio
import io
import sys
from typing import TYPE_CHECKING, Any, ClassVar, Self

import pytest

from pernix import Request, Response, Spider
from pernix.crawler import Crawler
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
    """A loopback server standing in for a proxy: it answers every page of the shop with its own name."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.server = LoopbackServer()
        for path in PATHS:
            self.server.route(path, f"<p>{name}</p>", headers=HTML)

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
