"""What `pernix fetch` reads from a Response: its text or Markdown, the parts selectors pick, or its links."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from parsel import Selector

from pernix.extraction import as_element, main_markdown, markdown, page_links, visible_text

if TYPE_CHECKING:
    from parsel import SelectorList

    from pernix.extraction import Element, Link
    from pernix.response import Response

__all__ = ["Extract", "Reading", "ReadingOptions", "Selection", "check_selection", "is_text", "read"]

type Extract = Literal["text", "markdown"]
"""What to read from a page or a part of it: its visible text, or Markdown."""

type Selection = tuple[Literal["css", "xpath"], str]
"""A selector: a CSS one, or an XPath expression."""

_TEXT_SUBTYPES = frozenset({"json", "xml", "javascript", "ecmascript", "x-www-form-urlencoded", "x-javascript"})
_HTML_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_PROBE = Selector(text="<html></html>")


@dataclass(frozen=True, slots=True)
class ReadingOptions:
    """What to read from each Response, as the options of `pernix fetch` ask.

    Attributes:
        extract: Read a page as its visible text, or its main content as Markdown;
            or each part the selectors pick, as its text or Markdown. `None` reads
            the body, or the parts' markup, as it is.
        selectors: Read the parts these pick instead of the whole page, in order.
        links: Read the links of the page, or of the parts the selectors pick,
            instead.
        max_chars: Characters to read at most.
    """

    extract: Extract | None = None
    selectors: tuple[Selection, ...] = ()
    links: bool = False
    max_chars: int | None = None


@dataclass(frozen=True, slots=True)
class Reading:
    """What was read from a Response.

    Attributes:
        body: The body's text, or what `extract` made of it; `None` for a binary
            body, for a body left out, and when selectors or links were read.
        matches: What the selectors picked, if they were given.
        links: The links, if they were asked for.
        truncated: Whether `max_chars` cut what was read.
    """

    body: str | None
    matches: list[str] | None = None
    links: list[Link] | None = None
    truncated: bool = False


def check_selection(selection: Selection) -> None:
    """Check that a selector is valid, before anything is fetched.

    Args:
        selection: The selector.

    Raises:
        ValueError: The selector is not valid.
    """
    kind, query = selection
    try:
        if kind == "css":
            _PROBE.css(query)
        else:
            _PROBE.xpath(query)
    except Exception as exc:  # cssselect and lxml raise errors of their own kinds
        raise ValueError(f"--{kind} {query!r} is not valid: {exc}") from exc


def read(response: Response, options: ReadingOptions, *, include_body: bool = True) -> Reading:
    """Read a Response as the options ask.

    Extraction applies to HTML documents; other text bodies are read as they are.
    Selectors and links apply to HTML and XML documents, and pick nothing in others.

    Args:
        response: The Response.
        options: What to read.
        include_body: Whether to read the body, when neither selectors nor links are
            asked for.

    Returns:
        What was read, cut at `options.max_chars`.
    """
    limit = options.max_chars
    if options.links:
        roots = _roots(response, options) if _is_document(response) else []
        found: dict[str, Link] = {}
        for root in roots:
            for link in page_links(root, urljoin=response.urljoin):
                if not (seen := found.get(link.url)) or not seen.text:
                    found[link.url] = link
        links, truncated = _first_links(list(found.values()), limit)
        return Reading(None, links=links, truncated=truncated)
    if options.selectors:
        matches = [_formatted(match, response, options.extract) for match in _matches(response, options)]
        matches, truncated = _first_chars(matches, limit)
        return Reading(None, matches=matches, truncated=truncated)
    body = _body(response, options.extract) if include_body else None
    if body is None:
        return Reading(None)
    if limit is not None and len(body) > limit:
        return Reading(body[:limit], truncated=True)
    return Reading(body)


def is_text(response: Response) -> bool:
    """Tell whether a body is text: by its media type, or, without one, by holding no NUL byte."""
    media_type = _media_type(response)
    if media_type is None:
        return b"\x00" not in response.body[:4096]
    kind, _, subtype = media_type.partition("/")
    return kind == "text" or subtype in _TEXT_SUBTYPES or subtype.endswith(("+json", "+xml"))


def _body(response: Response, extract: Extract | None) -> str | None:
    if not is_text(response):
        return None
    root = as_element(response.selector.root) if extract is not None and _is_html(response) else None
    if root is None:
        return response.text
    return visible_text(root) if extract == "text" else main_markdown(root, urljoin=response.urljoin)


def _matches(response: Response, options: ReadingOptions) -> list[Selector]:
    if not _is_document(response):
        return []
    found: list[Selector] = []
    for kind, query in options.selectors:
        selected: SelectorList[Selector] = response.css(query) if kind == "css" else response.xpath(query)
        found.extend(selected)
    return found


def _roots(response: Response, options: ReadingOptions) -> list[Element]:
    """The elements to read links in: those the selectors pick, or the whole document."""
    selectors = _matches(response, options) if options.selectors else [response.selector]
    return [root for selector in selectors if (root := as_element(selector.root)) is not None]


def _formatted(match: Selector, response: Response, extract: Extract | None) -> str:
    """A part a selector picked: its markup, or its text or Markdown; a string, such as `::text`, as it is."""
    element = as_element(match.root)
    if element is None or extract is None:
        return match.get()
    return visible_text(element) if extract == "text" else markdown(element, urljoin=response.urljoin)


def _is_document(response: Response) -> bool:
    """Whether a Response is an HTML or XML document, which selectors apply to."""
    return is_text(response) and response.selector.type in ("html", "xml")


def _is_html(response: Response) -> bool:
    media_type = _media_type(response)
    return media_type in _HTML_TYPES if media_type is not None else response.selector.type == "html"


def _media_type(response: Response) -> str | None:
    """The media type of the last `Content-Type` header, lower-cased, if any."""
    content_type = response.headers.get_all("Content-Type")
    return content_type[-1].partition(";")[0].strip().lower() if content_type else None


def _first_chars(values: list[str], limit: int | None) -> tuple[list[str], bool]:
    """The first values holding `limit` characters at most, the last one cut if needed; whether any were cut."""
    if limit is None:
        return values, False
    kept: list[str] = []
    left = limit
    for value in values:
        if len(value) > left:
            if left:
                kept.append(value[:left])
            return kept, True
        kept.append(value)
        left -= len(value)
    return kept, False


def _first_links(links: list[Link], limit: int | None) -> tuple[list[Link], bool]:
    """The first links whose URLs and texts hold `limit` characters at most; whether any were left out."""
    if limit is None:
        return links, False
    left = limit
    for index, link in enumerate(links):
        left -= len(link.url) + len(link.text)
        if left < 0:
            return links[:index], True
    return links, False
