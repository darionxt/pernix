"""The kind of request a Request is, as a browser would make it: a navigation, a form submission, a fetch, or a load.

A Request names no kind of its own: a GET is a top-level navigation, a POST whose
body is a form is a form submission, and anything else, such as a POST of JSON, is a
`fetch()`. A `Sec-Fetch-Dest` header names the kind instead: `document`, `iframe`,
`empty` for `fetch()` and `XMLHttpRequest`, `style`, `script`, or `image`. With the
pinned Profile, the HTTP Fetcher sends the headers Chrome sends for that kind
(`pernix.fetchers.chrome_headers`); the browser Fetcher only navigates.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pernix.headers import Headers
    from pernix.request import Request
    from pernix.response import Response

__all__ = ["DESTINATIONS", "FORM_TYPES", "RequestKind", "check_kind", "is_document", "referring_page", "request_kind"]

type RequestKind = Literal["navigation", "form", "iframe", "fetch", "style", "script", "image"]
"""A kind of request: a top-level navigation, a form submission, an iframe's navigation, a `fetch()`, or a load."""

DESTINATIONS: dict[str, RequestKind] = {
    "document": "navigation",
    "iframe": "iframe",
    "empty": "fetch",
    "style": "style",
    "script": "script",
    "image": "image",
}
"""The kinds by the `Sec-Fetch-Dest` that names them; `document` with a body is a form submission."""

FORM_TYPES = frozenset({"application/x-www-form-urlencoded", "multipart/form-data", "text/plain"})
"""The media types a form submits its body as."""

_LOADS = frozenset({"iframe", "style", "script", "image"})
"""The kinds that only ever GET."""


def request_kind(request: Request) -> RequestKind:
    """Tell the kind of a Request, from its `Sec-Fetch-Dest`, or else from its method and body.

    Args:
        request: The Request.

    Returns:
        The kind.

    Raises:
        ValueError: `Sec-Fetch-Dest` names no kind pernix knows.
    """
    return _kind(request.method, request.headers)


def check_kind(method: str, headers: Headers, *, render: bool) -> None:
    """Check that a Request's method and rendering fit the kind its headers name.

    Args:
        method: The method, upper-cased.
        headers: The headers.
        render: Whether the Request is rendered.

    Raises:
        ValueError: An iframe's navigation or a load is not a GET, or a rendered
            Request names another destination than `document`.
    """
    destination = _destination(headers)
    if destination is None:
        return
    kind = DESTINATIONS.get(destination)
    if kind in _LOADS and method != "GET":
        raise ValueError(f"a Request with Sec-Fetch-Dest: {destination} is a GET, as browsers make it; got {method}")
    if render and destination != "document":
        raise ValueError(
            f"the browser renders top-level navigations only, so a rendered Request cannot have "
            f"Sec-Fetch-Dest: {destination}"
        )


def is_document(kind: RequestKind) -> bool:
    """Whether a kind of request loads a document: a navigation, a form submission, or an iframe's navigation.

    Args:
        kind: The kind.

    Returns:
        Whether its response is a page.
    """
    return kind in ("navigation", "form", "iframe")


def referring_page(response: Response) -> str | None:
    """Return the page that the Requests made while handling a Response come from.

    That is the Response's own URL when it is a document, and otherwise the page
    its Request came from, as its `Referer` names it, so that the API calls paged
    from an API Response keep the page that started them.

    Args:
        response: The Response being handled.

    Returns:
        The page's URL, or `None` for none.
    """
    try:
        document = is_document(request_kind(response.request))
    except ValueError:
        document = False
    if document or response.request.fetcher == "browser" or response.request.render:
        return response.url
    return response.request.headers.get("Referer") or None


def _kind(method: str, headers: Headers) -> RequestKind:
    destination = _destination(headers)
    if destination is not None:
        kind = DESTINATIONS.get(destination)
        if kind is None:
            known = ", ".join(DESTINATIONS)
            raise ValueError(f"Sec-Fetch-Dest: {destination} names no kind of request pernix makes; use one of {known}")
        if kind == "navigation" and method != "GET":
            return "form"
        return kind
    if method == "GET":
        return "navigation"
    if method == "POST" and _media_type(headers) in FORM_TYPES:
        return "form"
    return "fetch"


def _destination(headers: Headers) -> str | None:
    value = headers.get("Sec-Fetch-Dest")
    return None if value is None else value.strip().lower()


def _media_type(headers: Headers) -> str:
    return (headers.get("Content-Type") or "").partition(";")[0].strip().lower()
