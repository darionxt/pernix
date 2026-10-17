"""Runs whose Sessions go between HTTP and the browser, against the loopback server."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, ClassVar

import pytest

from pernix import Account, Request, Response, Session, Spider
from pernix.testing import ReceivedRequest, Reply, crawl

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]
POOL: Mapping[str, Any] = {"sessions.enabled": True}

SCRIPTED_LOGIN = "<script>document.cookie = 'auth=rendered; path=/'</script><p>in</p>"

REMEMBER = """<html><body><p id="seen"></p><script>
document.getElementById("seen").textContent = localStorage.getItem("visit") || "first";
localStorage.setItem("visit", "again");
</script></body></html>"""


def _cookie_echo(request: ReceivedRequest) -> Reply:
    return Reply(f"<p>{request.header('Cookie') or ''}</p>", headers=HTML)


class Carry(Spider):
    """Logs in one way, then fetches the echo the other way, under the same Session."""

    settings: ClassVar[Mapping[str, Any]] = POOL

    def __init__(self, base: str, login_rendered: bool) -> None:
        self.base = base
        self.login_rendered = login_rendered

    async def start(self) -> AsyncIterator[Request]:
        yield Request(f"{self.base}/login", render=self.login_rendered, session="alice", callback=self.logged_in)

    async def logged_in(self, response: Response) -> AsyncIterator[Any]:
        yield Request(f"{self.base}/echo", render=not self.login_rendered, session=response.session)

    async def parse(self, response: Response) -> AsyncIterator[Any]:
        yield {"fetcher": response.fetcher, "session": response.session, "cookie": response.css("p::text").get()}


async def test_a_login_rendered_in_the_browser_carries_over_to_http(server: LoopbackServer) -> None:
    server.route("/login", SCRIPTED_LOGIN, headers=HTML)  # set by the page's script, as only a browser does
    server.route("/echo", handler=_cookie_echo)

    report = await crawl(Carry, base=server.url("").rstrip("/"), login_rendered=True)

    assert report.items == [{"fetcher": "http", "session": "alice", "cookie": "auth=rendered"}]


async def test_a_login_over_http_carries_over_to_rendered_requests(server: LoopbackServer) -> None:
    server.route("/login", "<p>in</p>", headers=[*HTML, ("Set-Cookie", "auth=http; HttpOnly")])
    server.route("/echo", handler=_cookie_echo)

    report = await crawl(Carry, base=server.url("").rstrip("/"), login_rendered=False)

    assert report.items == [{"fetcher": "browser", "session": "alice", "cookie": "auth=http"}]


async def test_local_storage_is_seen_by_the_next_rendered_request_of_its_session_only(server: LoopbackServer) -> None:
    server.route("/", REMEMBER, headers=HTML)

    class Remember(Spider):
        settings: ClassVar[Mapping[str, Any]] = {**POOL, "concurrency": 1}

        def __init__(self, url: str) -> None:
            self.url = url

        async def start(self) -> AsyncIterator[Request]:
            for number, session in enumerate(("alice", "alice", "bob")):
                yield Request(f"{self.url}?n={number}", render=True, session=session, priority=-number)

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"session": response.session, "seen": response.css("#seen::text").get()}

    report = await crawl(Remember, url=server.url("/"))

    assert report.items == [
        {"session": "alice", "seen": "first"},
        {"session": "alice", "seen": "again"},
        {"session": "bob", "seen": "first"},
    ]


async def test_a_login_may_render_its_pages(server: LoopbackServer) -> None:
    server.route("/login", SCRIPTED_LOGIN, headers=HTML)
    server.route("/echo", handler=_cookie_echo)

    class Rendered(Spider):
        settings: ClassVar[Mapping[str, Any]] = {
            **POOL,
            "sessions.accounts": [{"username": "alice", "password": "pw"}],
        }

        def __init__(self, base: str) -> None:
            self.base = base

        async def start(self) -> AsyncIterator[Request]:
            yield Request(f"{self.base}/echo")

        async def login(self, session: Session, account: Account | None) -> None:
            await self.fetch(Request(f"{self.base}/login", render=True))

        async def parse(self, response: Response) -> AsyncIterator[Any]:
            yield {"fetcher": response.fetcher, "cookie": response.css("p::text").get()}

    report = await crawl(Rendered, base=server.url("").rstrip("/"))

    assert report.items == [{"fetcher": "http", "cookie": "auth=rendered"}]
    assert report.stats["sessions.logins"] == 1
