from __future__ import annotations

from typing import TYPE_CHECKING, Any

import orjson

from pernix.browser import RenderResult
from pernix.fetchers import BROWSER_FETCHER
from pernix.headers import Headers
from pernix.request import Request
from pernix.response import Response
from pernix.urls import is_absolute

if TYPE_CHECKING:
    from pernix.headers import HeadersInput

__all__ = ["make_response"]

_HTML = "text/html; charset=utf-8"
_JSON = "application/json"


def make_response(
    url: str,
    body: str | bytes = b"",
    *,
    status: int = 200,
    headers: HeadersInput = None,
    json: Any = None,
    request: Request | None = None,
    encoding: str | None = None,
    browser: RenderResult | None = None,
) -> Response:
    """Build a Response for a test, as a Fetcher would.

    A Response to a rendered Request, or with `browser` given, is one the browser
    Fetcher would give: its `fetcher` is `browser`, and `response.browser` is empty
    unless given.

    Args:
        url: The absolute URL of the Response.
        body: The body; a `str` is encoded as UTF-8, and makes the Response HTML
            unless `headers` set a `Content-Type`.
        status: The status code.
        headers: The headers, as a mapping or as `(name, value)` pairs.
        json: A value sent as a JSON body, with `Content-Type: application/json`
            unless `headers` set one, instead of `body`.
        request: The Request it answers, whose Callback, `kwargs`, and `meta` a
            Callback sees; `Request(url)` by default, rendered if `browser` is
            given.
        encoding: Decode the body with this encoding, whatever it declares.
        browser: What rendering gave, such as captured responses or the results of
            `Evaluate` actions.

    Returns:
        The Response.

    Raises:
        ValueError: Both `body` and `json` are given, or `url` is relative.

    Example:
        >>> response = make_response("https://shop.example/books", "<a href='/book/1'>Dune</a>")
        >>> response.css("a::text").get()
        'Dune'
        >>> rendered = make_response(url, "<h1>Dune</h1>", browser=RenderResult(evaluated=[42]))
        >>> rendered.browser.evaluated
        (42,)
    """
    if not is_absolute(url):
        raise ValueError(f"a Response needs an absolute URL, got {url!r}")
    response_headers = Headers(headers)
    if json is not None:
        if body:
            raise ValueError("pass either body or json, not both")
        body = orjson.dumps(json)
        response_headers = _with_default(response_headers, _JSON)
    elif isinstance(body, str):
        body = body.encode()
        response_headers = _with_default(response_headers, _HTML)
    if request is None:
        request = Request(url, render=browser is not None)
    rendered = browser is not None or bool(request.render)
    return Response(
        url=url,
        status=status,
        headers=response_headers,
        body=body,
        request=request,
        encoding=encoding,
        fetcher=BROWSER_FETCHER if rendered else None,
        browser=(browser or RenderResult()) if rendered else None,
    )


def _with_default(headers: Headers, content_type: str) -> Headers:
    return headers if "Content-Type" in headers else Headers([*headers.items(), ("Content-Type", content_type)])
