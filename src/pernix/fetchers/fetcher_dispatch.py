from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, cast
from urllib.parse import urlsplit

from pernix.errors import ComponentError, SettingsError
from pernix.response import expect_response

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.fetchers.fetcher import Fetcher
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["BROWSER_FETCHER", "FetcherDispatch"]

BROWSER_FETCHER = "browser"
"""The name of the Fetcher that renders Requests with `render` set."""


class FetcherDispatch:
    """The innermost step of the download chain: fetches each Request with its Fetcher.

    The Fetcher is the one named by `request.fetcher`; otherwise the `browser`
    Fetcher if the Request needs rendering; otherwise the one whose `schemes` include
    the scheme of the URL. The Response records the name it was registered under.
    """

    def __init__(self, fetchers: Mapping[str, object]) -> None:
        """Check the Fetchers and index them by name and by URL scheme.

        Args:
            fetchers: The built Fetchers, by registered name.

        Raises:
            ComponentError: A component has no `async def fetch(self, request)`.
            SettingsError: Two Fetchers claim the same URL scheme.
        """
        for name, fetcher in fetchers.items():
            if not inspect.iscoroutinefunction(getattr(fetcher, "fetch", None)):
                raise ComponentError(f"the Fetcher {name!r}, {fetcher!r}, has no 'async def fetch(self, request)'")
        self._fetchers = cast("dict[str, Fetcher]", dict(fetchers))
        self._by_scheme: dict[str, str] = {}
        problems: list[str] = []
        for name, fetcher in self._fetchers.items():
            for scheme in sorted(getattr(fetcher, "schemes", frozenset[str]())):
                other = self._by_scheme.setdefault(scheme, name)
                if other != name:
                    problems.append(
                        f"fetchers: {other!r} and {name!r} both fetch {scheme!r} URLs; register a replacement "
                        f"under the name {other!r}, or disable one of them"
                    )
        if problems:
            raise SettingsError(problems)

    async def __call__(self, request: Request) -> Response:
        """Fetch a Request with the Fetcher it needs.

        Args:
            request: The Request.

        Returns:
            The Response, with `fetcher` set to the Fetcher's registered name.

        Raises:
            LookupError: No registered Fetcher fits the Request.
            TypeError: The Fetcher returned something other than a Response.
        """
        name = self._select(request)
        response = expect_response(await self._fetchers[name].fetch(request), f"the Fetcher {name!r}")
        return response if response.fetcher == name else response.replace(fetcher=name)

    def _select(self, request: Request) -> str:
        if request.fetcher is not None:
            if request.fetcher not in self._fetchers:
                raise LookupError(f"{request!r} names the Fetcher {request.fetcher!r}, which is not registered")
            return request.fetcher
        if request.render:
            if BROWSER_FETCHER not in self._fetchers:
                raise LookupError(f"{request!r} needs rendering, but the {BROWSER_FETCHER!r} Fetcher is not registered")
            return BROWSER_FETCHER
        scheme = urlsplit(request.url).scheme
        try:
            return self._by_scheme[scheme]
        except KeyError:
            raise LookupError(
                f"no registered Fetcher fetches {scheme!r} URLs such as {request!r}; register one with the "
                "fetchers setting, or name one with Request(fetcher=...)"
            ) from None
