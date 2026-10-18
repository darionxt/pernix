"""The parts of lxml elements that reading HTML uses, and how it sorts them."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = [
    "BLOCKS",
    "UNRENDERED",
    "Element",
    "as_element",
    "collapse_spaces",
    "is_hidden",
    "tag_name",
]


class Element(Protocol):
    """An lxml element, as parsel Selectors hold them.

    Attributes:
        tag: The tag name; a function for comments and processing instructions.
        text: The text before the first child.
        tail: The text after the element, up to its next sibling.
    """

    tag: object
    text: str | None
    tail: str | None

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the value of an attribute, or `default`."""
        ...

    def __iter__(self) -> Iterator[Element]: ...

    def iter(self, *tags: str) -> Iterator[Element]:
        """Iterate over the element and its descendants, in document order, those with one of `tags` if given."""
        ...

    def iterancestors(self) -> Iterator[Element]:
        """Iterate over the ancestors of the element, the closest first."""
        ...

    def itertext(self) -> Iterator[str]:
        """Iterate over the text of the element and its descendants, in document order."""
        ...


UNRENDERED = frozenset(
    {
        "audio",
        "canvas",
        "datalist",
        "embed",
        "head",
        "iframe",
        "noscript",
        "object",
        "script",
        "select",
        "style",
        "svg",
        "template",
        "video",
    }
)
"""Elements whose content a page does not show as text: scripts, styles, `<noscript>` with scripts on, and media."""

BLOCKS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "caption",
        "center",
        "dd",
        "details",
        "dialog",
        "dir",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "form",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hgroup",
        "hr",
        "html",
        "legend",
        "li",
        "main",
        "menu",
        "nav",
        "ol",
        "p",
        "pre",
        "search",
        "section",
        "summary",
        "table",
        "tbody",
        "td",
        "tfoot",
        "th",
        "thead",
        "tr",
        "ul",
    }
)
"""Elements laid out as blocks, which start and end a line."""

_SPACES = re.compile(r"\s+")

_HIDING_STYLE = re.compile(r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden)\s*(?:!\s*important\s*)?(?:;|$)")


def as_element(node: object) -> Element | None:
    """The element a node is, or `None` for a comment, a processing instruction, or what is no element at all.

    Args:
        node: A child of an element, or what a parsel Selector holds, such as the
            string of `::text`.

    Returns:
        The element, or `None`.
    """
    return cast("Element", node) if isinstance(getattr(node, "tag", None), str) else None


def tag_name(element: Element) -> str:
    """The lower-cased tag name of an element, without a namespace."""
    return cast("str", element.tag).rpartition("}")[2].lower()


def is_hidden(element: Element) -> bool:
    """Whether a page hides an element: with the `hidden` attribute, or an inline `display: none` or `visibility: hidden`."""
    if element.get("hidden") is not None:
        return True
    style = element.get("style")
    return style is not None and _HIDING_STYLE.search(style.lower()) is not None


def collapse_spaces(text: str | None) -> str:
    """Text with each run of whitespace turned into one space, as HTML lays out text outside `<pre>`."""
    return _SPACES.sub(" ", text) if text else ""
