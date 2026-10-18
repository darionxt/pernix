"""Finding the main content of an HTML document, apart from its navigation, footers, and other boilerplate."""

from __future__ import annotations

import functools
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pernix.extraction.elements import UNRENDERED, as_element, collapse_spaces, is_hidden, tag_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.extraction.elements import Element

__all__ = ["MainContent", "main_content"]

_BOILERPLATE_TAGS = frozenset({"aside", "dialog", "nav", "search"})

_BOILERPLATE_ROLES = frozenset(
    {
        "alertdialog",
        "banner",
        "complementary",
        "contentinfo",
        "dialog",
        "menu",
        "menubar",
        "navigation",
        "search",
        "toolbar",
    }
)
"""ARIA roles of the parts of a page around its content."""

_BOILERPLATE_NAMES = frozenset(
    {
        "ad",
        "ads",
        "advert",
        "advertisement",
        "breadcrumb",
        "breadcrumbs",
        "comments",
        "consent",
        "cookie",
        "cookies",
        "footer",
        "gdpr",
        "masthead",
        "menu",
        "modal",
        "nav",
        "navbar",
        "navigation",
        "newsletter",
        "pager",
        "pagination",
        "popup",
        "promo",
        "related",
        "share",
        "sharing",
        "sidebar",
        "social",
        "sponsor",
        "sponsored",
        "subscribe",
        "toolbar",
    }
)
"""Words of the classes and ids of boilerplate, such as `site-footer` or `cookie_banner`."""

_CONTENT_NAMES = frozenset({"article", "body", "content", "entry", "main", "post", "story", "text"})
"""Words that keep an element whose classes or ids also name boilerplate, such as `content-sidebar-wrap`."""

_SECTIONING = frozenset({"article", "aside", "main", "nav", "section"})
"""Elements inside which a `<header>` or `<footer>` belongs to them rather than to the page."""

_LINK_BLOCKS = frozenset({"div", "ol", "p", "section", "table", "ul"})
_LINK_DENSITY = 0.6
_MIN_LINKS = 3
_WORDS = re.compile(r"[a-z0-9]+")


@dataclass(frozen=True, slots=True)
class MainContent:
    """Where the main content of a document is, and what in it is boilerplate.

    Attributes:
        root: The element holding the main content.
        boilerplate: Whether an element inside `root` is boilerplate, to leave out
            with what it holds.
        prunes_links: Whether `boilerplate` takes blocks made mostly of links for
            boilerplate.
    """

    root: Element
    boilerplate: Callable[[Element], bool]
    prunes_links: bool


def main_content(document: Element, *, prune_links: bool = True) -> MainContent:
    """Find the main content of an HTML document.

    It is the document's `<main>`, or element with the `main` role, that holds
    text; otherwise its only `<article>`; otherwise its body. Inside it, navigation,
    asides, dialogs, search forms, the elements with the ARIA roles of those, of the
    page's banner, and of its footer, and those whose classes or ids name
    boilerplate, such as `cookie-banner` or `share`, are boilerplate. So are, in a
    body, its `<header>` and `<footer>`, and, with `prune_links`, the blocks made
    mostly of links, as menus are.

    Args:
        document: The document's root element.
        prune_links: Whether blocks made mostly of links are boilerplate in a body.

    Returns:
        Where the main content is.
    """
    body = next((node for node in map(as_element, document.iter("body")) if node is not None), document)
    root = _landmark(body)
    prunes_links = prune_links and root is None
    density = _LinkDensity(body) if prunes_links else None
    return MainContent(
        body if root is None else root,
        functools.partial(_is_boilerplate, in_body=root is None, density=density),
        prunes_links,
    )


def _landmark(body: Element) -> Element | None:
    """The `<main>` holding text, else the only `<article>`, if any."""
    articles: list[Element] = []
    for node in body.iter():
        element = as_element(node)
        if element is None or is_hidden(element):
            continue
        tag = tag_name(element)
        if (tag == "main" or "main" in _roles(element)) and any(text.strip() for text in element.itertext()):
            return element
        if tag == "article":
            articles.append(element)
    return articles[0] if len(articles) == 1 else None


def _is_boilerplate(element: Element, *, in_body: bool, density: _LinkDensity | None) -> bool:
    tag = tag_name(element)
    if tag in _BOILERPLATE_TAGS or _roles(element) & _BOILERPLATE_ROLES:
        return True
    words = set(_WORDS.findall(f"{element.get('class') or ''} {element.get('id') or ''}".lower()))
    if words & _BOILERPLATE_NAMES and not words & _CONTENT_NAMES:
        return True
    if in_body and tag in ("header", "footer") and _belongs_to_page(element):
        return True
    return density is not None and tag in _LINK_BLOCKS and density.mostly_links(element)


def _roles(element: Element) -> set[str]:
    return set((element.get("role") or "").lower().split())


def _belongs_to_page(element: Element) -> bool:
    """Whether a `<header>` or `<footer>` is the page's, not inside an article or a section."""
    return not any(tag_name(ancestor) in _SECTIONING for ancestor in element.iterancestors())


class _LinkDensity:
    """How much of the text of each element of a tree is in links, measured in one pass over it."""

    def __init__(self, root: Element) -> None:
        self._measured: dict[Element, tuple[int, int, int]] = {}
        self._measure(root)

    def mostly_links(self, element: Element) -> bool:
        """Whether most of an element's text is in links, of which it holds a few."""
        text, linked, links = self._measured.get(element, (0, 0, 0))
        return links >= _MIN_LINKS and text > 0 and linked / text > _LINK_DENSITY

    def _measure(self, element: Element) -> tuple[int, int, int]:
        """The characters of an element's text, those of it in links, and the links it holds."""
        text = _length(element.text)
        linked = links = 0
        for child in element:
            node = as_element(child)
            if node is not None and tag_name(node) not in UNRENDERED:
                child_text, child_linked, child_links = self._measure(node)
                text, linked, links = text + child_text, linked + child_linked, links + child_links
            text += _length(child.tail)
        if tag_name(element) == "a":
            linked, links = text, links + 1
        self._measured[element] = (text, linked, links)
        return text, linked, links


def _length(text: str | None) -> int:
    return len(collapse_spaces(text).strip())
