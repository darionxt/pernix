from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from parsel import Selector, SelectorList

if TYPE_CHECKING:
    from collections.abc import Mapping

    from pernix.headers import Headers
    from pernix.request import Request

__all__ = ["Response"]


class Response:
    """The result of one fetch, with lazily decoded text and selectors.

    Attributes:
        url: The final URL of the response.
        status: The HTTP status code.
        headers: The response headers, in wire order.
        body: The raw response body.
        request: The Request that produced this Response.
        cookies: Read-only cookies the client holds after the fetch, by name: those the
            Request carried (`cookies=` or a `Cookie` header) plus those set during the
            fetch, including on redirects.
            Pass them on with `Request(url, cookies=response.cookies)`.
    """

    __slots__ = ("_selector", "_text", "body", "cookies", "headers", "request", "status", "url")

    def __init__(
        self,
        *,
        url: str,
        status: int,
        headers: Headers,
        body: bytes,
        request: Request,
        cookies: Mapping[str, str] | None = None,
    ) -> None:
        """Create a Response. Fetchers build these; user code rarely needs to.

        Args:
            url: The final URL of the response.
            status: The HTTP status code.
            headers: The response headers.
            body: The raw response body.
            request: The Request that produced this Response.
            cookies: The cookies the client holds after the fetch, by name.
        """
        self.url = url
        self.status = status
        self.headers = headers
        self.body = body
        self.request = request
        self.cookies: Mapping[str, str] = MappingProxyType(dict(cookies or {}))
        self._text: str | None = None
        self._selector: Selector | None = None

    @property
    def meta(self) -> Mapping[str, Any]:
        """The read-only user data of the originating Request."""
        return self.request.meta

    @property
    def text(self) -> str:
        """The body decoded as UTF-8, with undecodable bytes replaced."""
        if self._text is None:
            self._text = self.body.decode("utf-8", errors="replace")
        return self._text

    @property
    def selector(self) -> Selector:
        """A selector over the decoded text, built on first use."""
        if self._selector is None:
            self._selector = Selector(text=self.text, base_url=self.url)
        return self._selector

    def css(self, query: str) -> SelectorList[Selector]:
        """Select elements with a CSS query.

        Args:
            query: A CSS selector, optionally with `::text` or `::attr(name)`.

        Returns:
            The matching selectors.
        """
        return self.selector.css(query)

    def xpath(self, query: str, **kwargs: Any) -> SelectorList[Selector]:
        """Select elements with an XPath query.

        Args:
            query: An XPath expression.
            **kwargs: Values for XPath variables used in `query`.

        Returns:
            The matching selectors.
        """
        return self.selector.xpath(query, **kwargs)

    def __repr__(self) -> str:
        return f"<Response {self.status} {self.url}>"
