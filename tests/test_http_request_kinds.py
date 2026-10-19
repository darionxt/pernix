"""The HTTP Fetcher sending each kind of request as Chrome does, with the pinned Profile, against the loopback server."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix import Cookie, Request, Session
from pernix.fetchers import HTTPFetcher, HTTPSettings

if TYPE_CHECKING:
    from pernix.testing import LoopbackServer


@pytest.mark.parametrize(
    ("page_host", "credentials"), [("127.0.0.1", True), ("localhost", False)], ids=["same origin", "another origin"]
)
async def test_a_fetch_takes_the_sessions_cookies_to_its_pages_origin_only(
    server: LoopbackServer, page_host: str, credentials: bool
) -> None:
    server.route("/api", "{}", method="POST", headers=[("Set-Cookie", "late=1")])
    session = Session(cookies=[Cookie(name="sid", value="1", domain="127.0.0.1")])
    page = server.url("/page").replace("127.0.0.1", page_host, 1)

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(
            Request(server.url("/api"), json={}, cookies={"own": "2"}, headers={"Referer": page}), session=session
        )

    sent = server.received[-1].header("Cookie")
    assert sent == ("sid=1; own=2" if credentials else "own=2")
    assert ("late" in {cookie.name for cookie in session.cookies}) == credentials
    assert server.received[-1].header("Origin") == page.removesuffix("/page")


async def test_a_form_redirected_to_a_get_goes_on_as_a_navigation(server: LoopbackServer) -> None:
    server.route("/submit", method="POST", status=303, headers=[("Location", "/done")])
    page = server.url("/form")

    async with HTTPFetcher() as fetcher:
        response = await fetcher.fetch(Request(server.url("/submit"), data={"a": "1"}, headers={"Referer": page}))

    assert response.url == server.url("/done")
    submitted, done = server.received
    assert (submitted.header("Origin"), submitted.header("Cache-Control")) == (server.url(""), "max-age=0")
    assert submitted.header("Content-Type") == "application/x-www-form-urlencoded"
    assert [done.header(name) for name in ("Origin", "Cache-Control", "Content-Type")] == [None, None, None]
    assert [done.header(name) for name in ("Sec-Fetch-Site", "Sec-Fetch-User", "Referer")] == [
        "same-origin",
        "?1",
        page,
    ]


async def test_a_destination_pernix_makes_no_request_for_fails_with_the_pinned_profile_only(
    server: LoopbackServer,
) -> None:
    server.route("/font", "")
    font = {"Sec-Fetch-Dest": "font"}

    async with HTTPFetcher() as fetcher:
        with pytest.raises(ValueError, match="names no kind of request pernix makes"):
            await fetcher.fetch(Request(server.url("/font"), headers=font))
        await fetcher.fetch(Request(server.url("/font"), headers=font, impersonate="chrome146"))

    assert server.received[-1].header("Sec-Fetch-Dest") == "font"


async def test_another_profile_sends_the_requests_referer_as_it_is(server: LoopbackServer) -> None:
    server.route("/next", "")
    page = "https://shop.example/books#top"

    async with HTTPFetcher(HTTPSettings(impersonate="chrome146")) as fetcher:
        await fetcher.fetch(Request(server.url("/next"), headers={"Referer": page}))

    assert server.received[-1].header("Referer") == page
    assert server.received[-1].header("Sec-Fetch-Site") == "none"


async def test_an_empty_referer_names_no_page(server: LoopbackServer) -> None:
    server.route("/api", "{}")

    async with HTTPFetcher() as fetcher:
        await fetcher.fetch(Request(server.url("/api"), headers={"Sec-Fetch-Dest": "empty", "Referer": ""}))

    received = server.received[-1]
    assert received.header("Referer") is None
    assert received.header("Sec-Fetch-Site") == "same-origin"
    assert received.header("Sec-Fetch-Mode") == "cors"
