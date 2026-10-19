"""Page methods and kept pages in the browser Fetcher, against the loopback server."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

import pytest

from pernix import Request, Retry, Session
from pernix.browser import Evaluate, PageMethod, Render
from pernix.errors import ActionError, ActionTimeout, PageCrashed
from pernix.fetchers import BrowserFetcher, BrowserSettings
from pernix.fetchers.kept_pages import keeping_pages
from pernix.stats import Stats

if TYPE_CHECKING:
    from patchright.async_api import Page, Route

    from pernix.testing import LoopbackServer

pytestmark = pytest.mark.usefixtures("browser_engine")

HTML = [("Content-Type", "text/html")]

STEPS = "<p id='steps'>steps:</p>"
NOTE = "step => { document.getElementById('steps').textContent += step + ';' }"


async def _note_the_method(page: Page) -> None:
    await page.evaluate(NOTE, "method")


async def test_setup_runs_before_the_page_loads(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async def add_header(page: Page) -> None:
        await page.set_extra_http_headers({"X-Setup": "yes"})

    async with BrowserFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/"), render=Render(setup=add_header)))

    assert server.received[0].header("X-Setup") == "yes"


async def test_the_action_method_runs_after_wait_for_and_the_actions(server: LoopbackServer) -> None:
    server.route("/", STEPS, headers=HTML)
    render = Render(
        wait_for="#steps",
        actions=[Evaluate(f"() => ({NOTE})('actions')")],
        action=_note_the_method,
        screenshot=True,
    )

    async with BrowserFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/"), render=render))

    assert response.css("#steps::text").get() == "steps:actions;method;"  # the Response shows what the method did
    assert len(response.browser.screenshots if response.browser else ()) == 1


async def test_the_routes_of_setup_are_tried_after_the_fetchers(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async def pass_everything(page: Page) -> None:
        async def go_on(route: Route) -> None:
            await route.continue_()

        await page.route("**/*", go_on)

    async with BrowserFetcher() as fetcher:
        request = Request(server.url("/"), method="POST", body=b"q=dune", render=Render(setup=pass_everything))
        await fetcher.fetch(request)

    [received] = server.received
    assert (received.method, received.body) == ("POST", b"q=dune")  # the first request was rewritten all the same


async def test_a_page_method_that_raises_fails_the_fetch_naming_it(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async def close_banner(page: Page) -> None:
        raise LookupError("no banner")

    async with BrowserFetcher() as fetcher:
        with pytest.raises(
            ActionError, match=r"Render\(action=.*close_banner\) on http://\S+ failed: LookupError"
        ) as e:
            await fetcher.fetch(Request(server.url("/"), render=Render(action=close_banner)))

    assert not isinstance(e.value, ActionTimeout)
    assert isinstance(e.value.__cause__, LookupError)


async def test_an_optional_page_method_that_fails_is_logged_and_skipped(
    server: LoopbackServer, caplog: pytest.LogCaptureFixture
) -> None:
    server.route("/", STEPS, headers=HTML)

    async def fails(page: Page) -> None:
        raise LookupError("no banner")

    render = Render(setup=PageMethod(fails, optional=True), action=_note_the_method)

    async with BrowserFetcher() as fetcher:
        with caplog.at_level(logging.WARNING, logger="pernix"):
            response = await fetcher.fetch(Request(server.url("/"), render=render))

    assert response.css("#steps::text").get() == "steps:method;"
    assert any(
        "Render(setup=" in record.getMessage() and "optional" in record.getMessage() for record in caplog.records
    )


async def test_a_page_method_taking_longer_than_its_timeout_is_an_action_timeout(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async def stalls(page: Page) -> None:
        await asyncio.sleep(30)

    async with BrowserFetcher() as fetcher:
        with pytest.raises(ActionTimeout, match=r"Render\(action=.*stalls\) .* took longer than 0.2 s"):
            await fetcher.fetch(Request(server.url("/"), render=Render(action=PageMethod(stalls, timeout=0.2))))


async def test_retry_raised_in_a_page_method_goes_through(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async def challenged(page: Page) -> None:
        raise Retry("a challenge page")

    async with BrowserFetcher() as fetcher:
        with pytest.raises(Retry, match="a challenge page"):
            await fetcher.fetch(Request(server.url("/"), render=Render(action=challenged)))


# Kept pages


async def test_a_page_is_kept_only_where_a_callback_holds_it(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async with BrowserFetcher() as fetcher:
        with pytest.raises(ValueError, match=r"keep_page=True.*an Inline Fetch, pernix fetch, the shell"):
            await fetcher.fetch(Request(server.url("/"), render=Render(keep_page=True)))

    assert server.received == []


async def test_a_kept_page_stays_open_until_its_scope_ends(server: LoopbackServer) -> None:
    server.route("/", "<button onclick=\"this.textContent = 'clicked'\">click</button>", headers=HTML)
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        async with keeping_pages():
            response = await fetcher.fetch(Request(server.url("/"), render=Render(keep_page=True)))
            page = response.page
            assert page is not None
            await page.click("button")
            assert await page.text_content("button") == "clicked"
        async with asyncio.timeout(10):
            while stats.get("browser.pages_open") != 0:  # noqa: ASYNC110  # given back once its context closed
                await asyncio.sleep(0.01)
        assert page.is_closed()
        assert stats.get("browser.contexts_open") == 0


async def test_a_page_not_kept_is_not_given(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async with BrowserFetcher() as fetcher, keeping_pages():
        response = await fetcher.fetch(Request(server.url("/"), render=True))

    assert response.page is None


async def test_the_fetches_made_while_a_page_is_kept_share_its_slot(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)
    stats = Stats()

    async with BrowserFetcher(BrowserSettings(max_pages=1), stats=stats) as fetcher, keeping_pages():
        kept = await fetcher.fetch(Request(server.url("/?kept"), render=Render(keep_page=True)))
        async with asyncio.timeout(10):  # the only slot is the kept page's
            other = await fetcher.fetch(Request(server.url("/?other"), render=True))

    assert (kept.status, other.status) == (200, 200)
    assert stats.get("browser.page_waits") is None


async def test_a_kept_page_under_a_session_keeps_what_its_fetch_changed_only(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=[*HTML, ("Set-Cookie", "loaded=1")])
    session = Session("alice")
    stats = Stats()

    async with BrowserFetcher(stats=stats) as fetcher:
        async with keeping_pages():
            response = await fetcher.fetch(Request(server.url("/"), render=Render(keep_page=True)), session=session)
            assert response.page is not None
            await response.page.evaluate("() => { document.cookie = 'clicked=1' }")
        await fetcher.fetch(Request(server.url("/"), render=True), session=session)

    assert {cookie.name for cookie in session.cookies} == {"loaded"}  # what the Callback changed is not kept
    assert stats.get("browser.contexts") == 2  # the kept page's context is not used again


async def test_a_kept_page_that_crashes_tells_its_scope(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)

    async with BrowserFetcher() as fetcher, keeping_pages() as keeping:
        response = await fetcher.fetch(Request(server.url("/"), render=Render(keep_page=True)))
        page = response.page
        assert page is not None
        crashed = asyncio.Event()
        page.on("crash", lambda _page: crashed.set())
        devtools = await page.context.new_cdp_session(page)
        crashing = asyncio.create_task(devtools.send("Page.crash"))  # it never answers
        async with asyncio.timeout(10):
            await crashed.wait()
        crashing.cancel()
        failure = await keeping.failure(RuntimeError("Page.title: Page crashed"))

    assert isinstance(failure, PageCrashed)
    assert isinstance(failure.__cause__, RuntimeError)


async def test_an_error_of_another_browser_leaves_a_kept_page_as_it_is(server: LoopbackServer) -> None:
    server.route("/", "<p>up</p>", headers=HTML)
    stats = Stats()
    elsewhere = Exception("Page.goto: Connection closed while reading from the driver")  # of a browser replaced

    async with BrowserFetcher(stats=stats) as fetcher, keeping_pages() as keeping:
        response = await fetcher.fetch(Request(server.url("/"), render=Render(keep_page=True)))
        failure = await keeping.failure(elsewhere)
        assert response.page is not None
        assert await response.page.text_content("p") == "up"

    assert failure is None
    assert stats.get("browser.lost") is None
