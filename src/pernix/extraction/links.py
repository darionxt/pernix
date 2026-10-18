"""The links of HTML, as absolute URLs with their text."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pernix.extraction.elements import as_element, collapse_spaces
from pernix.extraction.visible_text import visible_text

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.extraction.elements import Element

__all__ = ["Link", "page_links"]

_SCHEMES = frozenset({"http", "https"})


@dataclass(frozen=True, slots=True)
class Link:
    """A link of a page.

    Attributes:
        url: Where it leads, absolute.
        text: Its text; else its `aria-label`, `title`, or the alt text of an image in it.
    """

    url: str
    text: str


def page_links(root: Element, *, urljoin: Callable[[str], str]) -> list[Link]:
    """Return the http and https links inside an element, `<a href>` and `<area href>`, each URL once.

    Args:
        root: The element, such as a document's `<html>`.
        urljoin: Resolves their URLs, such as `Response.urljoin`.

    Returns:
        The links, in document order; a URL linked several times has the first text
        any of them has.
    """
    found: dict[str, str] = {}
    for node in root.iter("a", "area"):
        element = as_element(node)
        href = element.get("href") if element is not None else None
        if element is None or href is None or not href.strip():
            continue
        try:
            url = urljoin(href)
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            continue
        if scheme in _SCHEMES and not found.get(url):
            found[url] = _text(element)
    return [Link(url, text) for url, text in found.items()]


def _text(element: Element) -> str:
    text = collapse_spaces(visible_text(element)).strip()
    if text:
        return text
    for label in (element.get("aria-label"), element.get("title"), element.get("alt")):
        if label and label.strip():
            return collapse_spaces(label).strip()
    images = (as_element(node) for node in element.iter("img"))
    return next(
        (collapse_spaces(alt).strip() for image in images if image is not None and (alt := image.get("alt"))), ""
    )
