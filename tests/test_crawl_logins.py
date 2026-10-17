"""Runs whose Sessions log in with Accounts, against a loopback site with a login form."""

from __future__ import annotations

import asyncio
import io
import logging
import sys
import threading
import uuid
from typing import TYPE_CHECKING, Any, ClassVar
from urllib.parse import parse_qsl

import orjson
import pytest

from pernix import Account, Middleware, Request, Response, Session, Spider
from pernix.crawler import Crawler
from pernix.errors import SessionError
from pernix.settings import Secret
from pernix.testing import Reply, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping
    from pathlib import Path

    from pernix.middlewares import Download
    from pernix.shutdown import StopMode
    from pernix.testing import LoopbackServer, ReceivedRequest

PASSWORD = "s3cr3t-Pw-0451"
POOL: Mapping[str, Any] = {
    "sessions.enabled": True,
    "sessions.accounts": [{"id": "alice", "username": "alice@shop.example", "password": PASSWORD}],
}


class LoginSite:
    """A site whose pages need a login: a POST of the right credentials to `/login` hands out a session cookie.

    A login lasts `lifetime` pages, if given; a page asked for without one is a 401.
    """

    def __init__(self, server: LoopbackServer, *, lifetime: int | None = None) -> None:
        self.passwords = {"alice@shop.example": PASSWORD, "bob@shop.example": "bobs-pw"}
        self.lifetime = lifetime
        self.logins: list[str] = []
        self._served: dict[str, list[Any]] = {}  # session id -> [user, pages served]
        self._lock = threading.Lock()
        server.route("/login", method="POST", handler=self._log_in)
        server.route("/page", handler=self._page)

    def _log_in(self, request: ReceivedRequest) -> Reply:
        form = dict(parse_qsl(request.body.decode()))
        user = form.get("user", "")
        if self.passwords.get(user) != form.get("password"):
            return Reply(status=403, body="wrong credentials")
        sid = uuid.uuid4().hex
        with self._lock:
            self.logins.append(user)
            self._served[sid] = [user, 0]
        return Reply(body=f"welcome {user}", headers=[("Set-Cookie", f"sid={sid}")])

    def _page(self, request: ReceivedRequest) -> Reply:
        cookie = request.header("Cookie") or ""
        sid = cookie.removeprefix("sid=")
        with self._lock:
            served = self._served.get(sid)
            if served is None or (self.lifetime is not None and served[1] >= self.lifetime):
                return Reply(status=401, body="log in first")
            served[1] += 1
            return Reply(body=f"hello {served[0]}")


class Shop(Spider):
    """Fetches `/page` a number of times, logging its Sessions in with their Accounts."""

    def __init__(self, base: str, pages: int = 1) -> None:
        self.base = base
        self.pages = pages

    async def start(self) -> AsyncIterator[Request]:
        for number in range(self.pages):
            yield Request(f"{self.base}/page?n={number}")

    async def login(self, session: Session, account: Account | None) -> None:
        assert account is not None
        data = {"user": account.username, "password": account.password.reveal()}
        page = await self.fetch(Request(f"{self.base}/login", data=data))
        if page.status != 200:
            raise RuntimeError(f"cannot log in as {account.id}: {page.status}")

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"session": response.session, "text": response.text}


def _base(server: LoopbackServer) -> str:
    return server.url("").rstrip("/")


@pytest.mark.parametrize("max_concurrency", [1, 4])
async def test_concurrent_requests_under_one_account_log_in_exactly_once(
    server: LoopbackServer, max_concurrency: int
) -> None:
    site = LoginSite(server)
    accounts = [{**POOL["sessions.accounts"][0], "max_concurrency": max_concurrency}]

    report = await asyncio.wait_for(
        crawl(Shop, base=_base(server), pages=20, settings={**POOL, "sessions.accounts": accounts, "concurrency": 8}),
        timeout=20,
    )

    assert site.logins == ["alice@shop.example"]
    assert report.items == [{"session": "alice", "text": "hello alice@shop.example"}] * 20
    assert report.stats["sessions.logins"] == 1
    assert "sessions.login_failures" not in report.stats


async def test_a_session_logs_in_again_once_the_site_expires_its_login(server: LoopbackServer) -> None:
    site = LoginSite(server, lifetime=3)

    report = await crawl(Shop, base=_base(server), pages=7, settings=POOL)

    assert report.items == [{"session": "alice", "text": "hello alice@shop.example"}] * 7
    assert site.logins == ["alice@shop.example"] * 3
    assert report.stats["sessions.logins"] == 3
    assert report.stats["sessions.outcomes.auth_expired"] == 2
    assert report.stats["requests.retried"] == 2  # each logged-out page was retried once


async def test_account_credentials_never_appear_in_the_logs_or_stats(
    server: LoopbackServer, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    LoginSite(server)
    stream = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stream)
    accounts_file = tmp_path / "accounts.jsonl"
    accounts_file.write_text(orjson.dumps({"username": "bob@shop.example", "password": "bobs-pw"}).decode() + "\n")

    class Careless(Shop):
        async def login(self, session: Session, account: Account | None) -> None:
            assert account is not None
            self.logger.info("logging in with %s", account.password.reveal())
            await super().login(session, account)
            if account.id != "alice":
                raise RuntimeError(f"refusing {account.password.reveal()!r}")

    crawler = Crawler.from_spider(
        Careless,
        args={"base": _base(server), "pages": 4},
        settings={
            **POOL,
            "sessions.accounts_file": str(accounts_file),
            "sessions.cooldown": 0,
            "log.output": "stderr",
            "log.format": "json",
            "log.level": "DEBUG",
        },
    )
    async with asyncio.timeout(20):
        await crawler.crawl()

    output = stream.getvalue()
    assert "logging in with ***" in output
    assert "refusing '***'" in output
    for secret in (PASSWORD, "bobs-pw"):
        assert secret not in output
        assert all(secret not in f"{key}={value}" for key, value in crawler.stats.snapshot().items())


async def test_a_failing_login_cools_its_session_down_then_retires_it(server: LoopbackServer) -> None:
    site = LoginSite(server)
    site.passwords["alice@shop.example"] = "changed"
    failures: list[Exception] = []

    class Pinned(Shop):
        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/page", session="alice", errback=self.failed)

        async def failed(self, request: Request, error: Exception) -> AsyncIterator[Any]:
            failures.append(error)
            yield None

    report = await asyncio.wait_for(
        crawl(Pinned, base=_base(server), settings={**POOL, "sessions.cooldown": 0.05}), timeout=20
    )

    assert len(site.logins) == 0
    assert report.stats["sessions.login_failures"] == 3
    assert [type(error) for error in failures] == [SessionError]
    assert "retired" in str(failures[0])
    assert "sessions.logins" not in report.stats


async def test_a_login_that_does_not_work_is_retried_only_so_often(server: LoopbackServer) -> None:
    LoginSite(server)

    class Forgetful(Shop):
        async def login(self, session: Session, account: Account | None) -> None:
            pass  # never logs in, so every page is a 401

    report = await asyncio.wait_for(
        crawl(Forgetful, base=_base(server), pages=3, settings={**POOL, "sessions.cooldown": 0, "retry.times": 10}),
        timeout=20,
    )

    assert report.items == []
    assert report.stats["sessions.logins"] == 3
    assert report.stats["sessions.login_failures"] == 3  # the Session was Retired then
    assert report.stats["requests.failed"] == 3


async def test_accounts_come_from_the_settings_a_file_and_the_spider(server: LoopbackServer, tmp_path: Path) -> None:
    LoginSite(server)
    accounts_file = tmp_path / "accounts.csv"
    accounts_file.write_text("id,username,password\nbob,bob@shop.example,bobs-pw\n")
    imported: list[str] = []

    class Imported(Shop):
        async def accounts(self) -> AsyncIterator[Account]:
            async for account in super().accounts():
                yield account
            yield Account(username="carol@shop.example", password=Secret("carols-pw"), id="carol")

        async def login(self, session: Session, account: Account | None) -> None:
            assert account is not None
            imported.append(account.id)
            if account.id != "carol":
                await super().login(session, account)

        async def start(self) -> AsyncIterator[Request]:
            for name in ("alice", "bob"):
                yield Request(f"{self.base}/page?as={name}", session=name)

    report = await crawl(Imported, base=_base(server), settings={**POOL, "sessions.accounts_file": str(accounts_file)})

    assert sorted(imported) == ["alice", "bob"]  # carol's Session was never leased
    assert sorted(item["text"] for item in report.items) == ["hello alice@shop.example", "hello bob@shop.example"]


async def test_sessions_without_an_account_log_in_too(server: LoopbackServer) -> None:
    server.route("/guest", handler=lambda _: Reply(headers=[("Set-Cookie", "guest=1")]))
    server.route("/page", handler=lambda request: Reply(body=request.header("Cookie") or ""))
    accounts: list[Account | None] = []

    class Guest(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"sessions.enabled": True, "sessions.size": 1}

        def __init__(self, base: str) -> None:
            self.base = base

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/page")

        async def login(self, session: Session, account: Account | None) -> None:
            accounts.append(account)
            await self.fetch(f"{self.base}/guest")

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"cookie": response.text}

    report = await crawl(Guest, base=_base(server))

    assert accounts == [None]
    assert report.items == [{"cookie": "guest=1"}]


async def test_a_login_shares_the_place_of_its_fetch_in_a_full_slot(server: LoopbackServer) -> None:
    LoginSite(server)

    report = await asyncio.wait_for(
        crawl(Shop, base=_base(server), pages=3, settings={**POOL, "domain.concurrency": 1}), timeout=10
    )

    assert len(report.items) == 3


async def test_an_inline_fetch_logs_its_session_in_again_in_place(server: LoopbackServer) -> None:
    site = LoginSite(server, lifetime=1)

    class Inline(Shop):
        async def parse(self, response: Response) -> AsyncIterator[Any]:
            again = await self.fetch(f"{self.base}/page?again=1")  # continues the logged-out Session
            yield {"first": response.text, "again": again.text, "session": again.session}

    report = await crawl(Inline, base=_base(server), settings=POOL)

    assert report.items == [
        {"first": "hello alice@shop.example", "again": "hello alice@shop.example", "session": "alice"}
    ]
    assert len(site.logins) == 2


async def test_a_fetch_nested_in_a_middleware_has_the_page_fetch_log_in_again(server: LoopbackServer) -> None:
    site = LoginSite(server, lifetime=2)

    class Peek(Middleware):
        """Fetches each page once more first, sharing the Lease of the page fetch."""

        def __init__(self, crawler: Crawler) -> None:
            self.crawler = crawler

        async def __call__(self, request: Request, call_next: Download) -> Response:
            if "/page" in request.url and "peek" not in request.url:
                await self.crawler.fetch(f"{request.url}&peek=1")
            return await call_next(request)

    report = await crawl(Shop, base=_base(server), pages=3, settings={**POOL, "middlewares": {Peek: 100}})

    assert report.items == [{"session": "alice", "text": "hello alice@shop.example"}] * 3
    assert len(site.logins) == 3  # the peeks the logins expired on were not retried under the expired login
    assert report.stats["requests.failed"] == 0


@pytest.mark.parametrize("mode", ["force", "drain"])
async def test_a_stop_ends_a_run_whose_accounts_take_forever_to_import(mode: StopMode) -> None:
    importing = asyncio.Event()

    class Hanging(Spider):
        settings: ClassVar[Mapping[str, Any]] = {"sessions.enabled": True, "shutdown.grace": 0.05}

        async def accounts(self) -> AsyncIterator[Account]:
            importing.set()
            await asyncio.Event().wait()  # a database that never answers
            yield Account(username="alice", password=Secret(PASSWORD))

    crawler = Crawler.from_spider(Hanging)
    run = asyncio.create_task(crawler.crawl())
    await importing.wait()
    crawler.stop(mode)  # a Drain turns into a Force Stop once its grace is over

    result = await asyncio.wait_for(run, timeout=5)

    assert result.reason == "forced"


async def test_session_hooks_without_a_session_pool_are_warned_about(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/page", "page")

    with caplog.at_level(logging.WARNING, logger="pernix"):
        report = await crawl(Shop, base=_base(server))

    assert len(report.items) == 1
    assert any("defines login(), but no Session Pool is enabled" in message for message in caplog.messages)
