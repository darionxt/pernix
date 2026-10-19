"""The cookies of one HTTP fetch, sent to each hop of its redirect chain as a browser sends them."""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

from pernix.cookie_jar import Cookie, CookieJar, cookie_key
from pernix.cookies import request_cookies, requires_secure
from pernix.urls import url_host

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.request import Request
    from pernix.session import Session

__all__ = ["ChainCookies"]

_NOT_IN_HEADER = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")
"""The characters a cookie cannot carry in a `Cookie` header: the control characters but the tab."""


class ChainCookies:
    """The cookies one fetch sends to each hop of its redirect chain, and those its hops set.

    They start as the Session's cookies, if any, joined by the Request's own, given
    as `cookies=` or as a `Cookie` header. Those are held for the Request's host
    only, for every path, as Secure cookies when their names have a prefix browsers
    take only on Secure cookies; one replacing a Session cookie held so keeps its
    place. The `Set-Cookie` headers of each hop join them by the cookie jar's rules,
    and go to the Session too, which never keeps the Request's own cookies.
    """

    def __init__(self, request: Request, session: Session | None) -> None:
        """Gather the cookies a fetch starts with.

        Args:
            request: The Request fetched.
            session: The Session it is sent under, if any.
        """
        self._session = session
        self._jar = CookieJar(() if session is None else session.cookies)
        self._own = CookieJar()
        host = url_host(request.url)
        if host is not None:  # a URL naming no host, such as http:///path, holds none
            self._hold(host, request_cookies(request))

    def header(self, url: str, *, credentials: bool = True) -> str | None:
        """Return the cookies to send to a hop, as the value of its `Cookie` header.

        Args:
            url: The URL of the hop.
            credentials: Whether the hop takes the cookies of the Session and of the
                chain; without, only the Request's own go, as a `fetch()` sends no
                others to another origin than its page's.

        Returns:
            The cookies matching the URL, in the order a browser sends them: longer
            paths first, then the earliest set first. Cookies a header cannot carry,
            with a control character other than the tab, are left out. `None` for no
            cookie.
        """
        pairs = [
            f"{cookie.name}={cookie.value}"
            for cookie in (self._jar if credentials else self._own).matching(url)
            if not _NOT_IN_HEADER.search(cookie.name + cookie.value)
        ]
        return "; ".join(pairs) or None

    def receive(self, url: str, set_cookie_headers: list[str]) -> None:
        """Apply the `Set-Cookie` headers of a hop's response, in the Session too.

        Args:
            url: The URL of the hop.
            set_cookie_headers: The `Set-Cookie` header values.
        """
        now = time.time()
        self._jar.receive(url, set_cookie_headers, now=now)
        if self._session is not None:
            self._session.cookies.receive(url, set_cookie_headers, now=now)

    def _hold(self, host: str, cookies: Mapping[str, str]) -> None:
        """Hold the Request's own cookies for its host, in the order given, each replacing a cookie in its place."""
        held = {cookie_key(cookie): cookie for cookie in self._jar}
        now = time.time()
        for name, value in cookies.items():
            replaced = held.get((host, True, "/", name))
            created = now if replaced is None else replaced.created
            cookie = Cookie(
                name=name, value=value, domain=host, secure=requires_secure(name), created=created, modified=now
            )
            self._jar.set(cookie)
            self._own.set(cookie)
