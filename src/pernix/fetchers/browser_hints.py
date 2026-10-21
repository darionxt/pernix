"""Client hints between a Session and a browser context: teaching the context those the Session keeps.

Chrome keeps the client hints an origin asked for with its profile, and offers no
other way to set them than a response's `Accept-CH`. A context learns those of a
Session's origins from a navigation to each that a route answers in the browser
itself, with an `Accept-CH` naming them, or an empty one to forget them, so the
request never leaves the browser; the page bypasses service workers, which would
take the navigation before the route. Chrome still connects to the origin ahead
of the navigation, as it does for every navigation: one TLS handshake, which the
next navigation there may use, and no request.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from patchright.async_api import BrowserContext, CDPSession, Response, Route

__all__ = ["accepted_hints", "teach_hints"]

_TEACHING_TIMEOUT = 10.0
"""Seconds the navigation that teaches a context an origin's client hints may take; it never leaves the browser."""


async def teach_hints(context: BrowserContext, origin: str, hints: Sequence[str]) -> None:
    """Make a context keep the client hints an origin asked for, in place of those it kept.

    Args:
        context: The browser context.
        origin: The origin, such as `https://example.com`.
        hints: The hints; none to forget those it kept.

    Raises:
        Exception: The page could not be opened, the navigation did not commit, or
            something else than the route answered it.
    """
    headers = {"Content-Type": "text/html", "Accept-CH": ", ".join(hints)}
    answered: list[str] = []

    async def answer(route: Route) -> None:
        if route.request.is_navigation_request():
            answered.append(route.request.url)
        await route.fulfill(status=200, headers=headers, body="")

    page = await context.new_page()
    devtools: CDPSession | None = None
    try:
        devtools = await context.new_cdp_session(page)
        await devtools.send("Network.enable")  # pyright: ignore[reportUnknownMemberType]  # untyped
        await devtools.send("Network.setBypassServiceWorker", {"bypass": True})  # pyright: ignore[reportUnknownMemberType]
        await page.route("**/*", answer)
        await page.goto(f"{origin}/", wait_until="commit", timeout=_TEACHING_TIMEOUT * 1000)
    finally:
        try:
            if devtools is not None:
                await devtools.detach()  # attached, it could keep the page from closing, which then hung the fetch
        finally:
            await page.close()
    if not answered:
        raise RuntimeError(f"the navigation that teaches the client hints of {origin} was answered elsewhere")


async def accepted_hints(navigations: Sequence[Response]) -> list[tuple[str, str]]:
    """Return what the responses to a page's navigations asked for with `Accept-CH`.

    Args:
        navigations: The responses to the navigations of the page's main frame, its
            redirects included, in order.

    Returns:
        The URL of each response with an `Accept-CH`, and its value, its fields
        joined with commas; those that can no longer be read are left out.
    """
    accepted: list[tuple[str, str]] = []
    for response in navigations:
        try:
            value = await response.header_value("accept-ch")
        except Exception:  # noqa: S112  # a response the browser no longer holds
            continue
        if value is not None:
            accepted.append((response.url, value))
    return accepted
