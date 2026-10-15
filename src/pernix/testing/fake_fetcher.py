from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, cast

from pernix.response import Response
from pernix.testing.responses import make_response

if TYPE_CHECKING:
    import re
    from collections.abc import Mapping

    from pernix.request import Request

__all__ = ["Answer", "FakeFetcher"]

type Answer = str | bytes | Response | Exception | Callable[[Request], Any]
"""How a FakeFetcher answers a Request: a body for a 200 Response, a Response, an
exception to raise, or a function of the Request returning, or awaiting, one of
those."""


@dataclass(slots=True)
class _Route:
    key: str | re.Pattern[str]
    answers: list[Answer]
    method: str | None
    taken: int = 0

    def matches(self, request: Request) -> bool:
        if self.method is not None and request.method != self.method:
            return False
        if isinstance(self.key, str):
            return request.url == self.key
        return self.key.fullmatch(request.url) is not None

    def next_answer(self) -> Answer:
        answer = self.answers[min(self.taken, len(self.answers) - 1)]
        self.taken += 1
        return answer


class FakeFetcher:
    r"""A Fetcher answering Requests from canned answers, so Spiders can be tested without a network.

    A route answers the Requests whose URL is its key, or fully matches it when the
    key is a compiled pattern; the first route added that matches answers. A list of
    answers is used in turn, its last answer repeating. Requests no route matches
    are answered with 404 and recorded in `unmatched`.

    It fetches `http` and `https` URLs, so registered as `http` in the `fetchers`
    setting it stands in for the HTTP Fetcher; `pernix.testing.crawl(fetcher=...)`
    does that.

    Attributes:
        schemes: The URL schemes it fetches.
        requests: Every Request it fetched, in order.
        unmatched: The Requests no route matched.

    Example:
        >>> fake = FakeFetcher({
        ...     "https://shop.example/": "<a href='/book/1'>Dune</a>",
        ...     re.compile(r"https://shop\.example/book/\d+"): "<h1>Dune</h1>",
        ...     "https://shop.example/flaky": [Timeout("slow"), "<p>ok</p>"],
        ... })
    """

    schemes: ClassVar[frozenset[str]] = frozenset({"http", "https"})

    def __init__(self, routes: Mapping[str | re.Pattern[str], Answer | list[Answer]] | None = None) -> None:
        """Create the Fetcher with routes, in order.

        Args:
            routes: Answers by URL, or by compiled pattern.
        """
        self._routes: list[_Route] = []
        self.requests: list[Request] = []
        self.unmatched: list[Request] = []
        for key, answer in (routes or {}).items():
            self.add(key, answer)

    def add(self, key: str | re.Pattern[str], answer: Answer | list[Answer], *, method: str | None = None) -> None:
        """Add a route, after those already added.

        Args:
            key: The URL to answer, or a compiled pattern the whole URL must match.
            answer: The answer, or a list of answers to use in turn, the last one
                repeating.
            method: Answer only Requests with this HTTP method.

        Raises:
            ValueError: `answer` is an empty list.
        """
        answers: list[Answer] = cast("list[Answer]", answer[:]) if isinstance(answer, list) else [answer]
        if not answers:
            raise ValueError(f"the route {key!r} needs at least one answer")
        self._routes.append(_Route(key, answers, None if method is None else method.upper()))

    async def fetch(self, request: Request) -> Response:
        """Answer a Request from the first route that matches it.

        Args:
            request: The Request.

        Returns:
            The answer, as a Response to `request`; 404 if no route matches.

        Raises:
            Exception: The answer is an exception.
            TypeError: The answer is none of those a route may give.
        """
        self.requests.append(request)
        route = next((route for route in self._routes if route.matches(request)), None)
        if route is None:
            self.unmatched.append(request)
            return make_response(request.url, b"", status=404, request=request)
        answer = route.next_answer()
        if callable(answer):
            answer = answer(request)
            if inspect.isawaitable(answer):
                answer = await answer
        return _respond(answer, request)


def _respond(answer: object, request: Request) -> Response:
    match answer:
        case str() | bytes():
            return make_response(request.url, answer, request=request)
        case Response():
            return answer.replace(request=request)
        case Exception():
            raise answer.with_traceback(None)  # each time anew, rather than with the frames of every raise
        case _:
            raise TypeError(
                f"a FakeFetcher route answered {request!r} with {answer!r}; answer with a body, a Response, "
                "an exception, or a function returning one of those"
            )
