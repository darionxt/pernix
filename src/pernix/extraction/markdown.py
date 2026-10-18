"""HTML as Markdown: headings, paragraphs, lists, links, tables, and code blocks."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from pernix.extraction.elements import BLOCKS, UNRENDERED, as_element, collapse_spaces, is_hidden, tag_name
from pernix.extraction.main_content import main_content

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from pernix.extraction.elements import Element

__all__ = ["main_markdown", "markdown"]

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}
_EMPHASIS = {"b": "**", "strong": "**", "em": "*", "i": "*", "del": "~~", "s": "~~", "strike": "~~"}
_CODE = frozenset({"code", "kbd", "samp", "tt"})
_LISTS = frozenset({"ul", "ol", "menu", "dir"})
_LEFT_OUT = UNRENDERED | {"button", "input", "option", "textarea"}
"""Elements left out of Markdown: those not rendered as text, and form controls."""

_LINKED_SCHEMES = frozenset({"http", "https", "mailto", "tel", "ftp"})
_IMAGE_SCHEMES = frozenset({"http", "https"})

_BREAK = "\x00"
"""Stands for a `<br>` until the whitespace around it collapsed."""

_SPACES_AROUND_BREAKS = re.compile(" *\x00 *")
_SPACES = re.compile(" {2,}")
_LANGUAGE = re.compile(r"(?:^|\s)(?:language|lang)-([\w#+.-]+)")
_LAYOUT = frozenset({"blockquote", "div", "dl", "h1", "h2", "h3", "h4", "h5", "h6", "ol", "p", "pre", "table", "ul"})
"""Elements that make a table one laid out for the page rather than holding data, when its cells hold them."""


def markdown(root: Element, *, urljoin: Callable[[str], str], skip: Callable[[Element], bool] | None = None) -> str:
    """Return the content of an element as Markdown.

    Headings, paragraphs, lists, block quotes, tables with a header row, code
    blocks with their language, emphasis, inline code, links, and images with alt
    text are kept, with their URLs made absolute; other elements give their text.
    Scripts, styles, media, form controls, and hidden elements are left out. Text is
    escaped only where it would break a link or a table cell.

    Args:
        root: The element.
        urljoin: Resolves the URLs of links and images, such as `Response.urljoin`.
        skip: Leaves out the elements it is true of, with what they hold.

    Returns:
        The Markdown, its blocks separated by blank lines.
    """
    return "\n\n".join(_Writer(urljoin, skip).element(root))


def main_markdown(document: Element, *, urljoin: Callable[[str], str]) -> str:
    """Return the main content of a document as Markdown, without its boilerplate.

    See `pernix.extraction.main_content()`. When leaving out the blocks made mostly
    of links leaves nothing, as on a page that is a list of links, they are kept.

    Args:
        document: The document's root element.
        urljoin: Resolves the URLs of links and images, such as `Response.urljoin`.

    Returns:
        The Markdown.
    """
    content = main_content(document)
    text = markdown(content.root, urljoin=urljoin, skip=content.boilerplate)
    if not text and content.prunes_links:
        content = main_content(document, prune_links=False)
        text = markdown(content.root, urljoin=urljoin, skip=content.boilerplate)
    return text


class _Writer:
    """Writes the Markdown of elements: blocks, made of inline content."""

    def __init__(self, urljoin: Callable[[str], str], skip: Callable[[Element], bool] | None) -> None:
        self._urljoin = urljoin
        self._skip = skip

    def element(self, element: Element) -> list[str]:
        """The Markdown blocks of an element: a block, such as a table, or a paragraph of inline content."""
        if tag_name(element) in _LEFT_OUT or is_hidden(element):
            return []
        if self._is_block(element):
            return self._block(element)
        text = _finished(self._inline(element))
        return [text] if text else []

    def blocks(self, element: Element) -> list[str]:
        """The Markdown blocks of an element's content; runs of inline content become paragraphs."""
        blocks: list[str] = []
        run = [collapse_spaces(element.text)]
        for child, tail in self._children(element):
            if child is not None:
                if self._is_block(child):
                    _add_paragraph(blocks, run)
                    blocks.extend(self._block(child))
                else:
                    run.append(self._inline(child))
            run.append(collapse_spaces(tail))
        _add_paragraph(blocks, run)
        return blocks

    def _children(self, element: Element) -> Iterator[tuple[Element | None, str | None]]:
        """The children of an element that are written, or `None` for those that are not, with their tails."""
        for child in element:
            node = as_element(child)
            if node is not None and (tag_name(node) in _LEFT_OUT or is_hidden(node) or self._skipped(node)):
                node = None
            yield node, child.tail

    def _skipped(self, element: Element) -> bool:
        return self._skip is not None and self._skip(element)

    def _is_block(self, element: Element) -> bool:
        tag = tag_name(element)
        if tag in BLOCKS:
            return True
        if tag in ("a", "br", "img") or tag in _CODE:
            return False
        return any(tag_name(node) in BLOCKS for node in _descendants(element))  # such as a <span> holding <div>s

    def _block(self, element: Element) -> list[str]:
        tag = tag_name(element)
        if tag in _HEADINGS:
            text = _one_line(self._inline_text(element))
            return [f"{'#' * _HEADINGS[tag]} {text}"] if text else []
        if tag in _LISTS:
            return self._list(element, ordered=tag == "ol")
        if tag == "pre":
            return _code_block(element)
        if tag == "blockquote":
            quoted = "\n\n".join(self.blocks(element))
            return ["\n".join(f"> {line}" if line else ">" for line in quoted.split("\n"))] if quoted else []
        if tag == "table":
            return self._table(element)
        if tag == "hr":
            return ["---"]
        if tag == "dt":
            text = _one_line(self._inline_text(element))
            return [f"**{text}**"] if text else []
        return self.blocks(element)

    def _list(self, element: Element, *, ordered: bool) -> list[str]:
        number = _start(element) if ordered else 0
        items: list[str] = []
        for child, _ in self._children(element):
            if child is None:
                continue
            if tag_name(child) == "li" or self._is_block(child):
                content = "\n".join(self.blocks(child) if tag_name(child) == "li" else self._block(child))
            else:
                content = _finished(self._inline(child))
            if not content:
                continue
            marker = f"{number}." if ordered else "-"
            number += 1
            items.append(f"{marker} {_indented(content, ' ' * (len(marker) + 1))}")
        return ["\n".join(items)] if items else []

    def _table(self, table: Element) -> list[str]:
        rows = [cells for row in self._rows(table) if (cells := self._cells(row))]
        caption = next(
            (node for node, _ in self._children(table) if node is not None and tag_name(node) == "caption"), None
        )
        if not rows or (len(rows) == 1 and len(rows[0]) == 1) or any(_lays_out(cell) for row in rows for cell in row):
            return self.blocks(table)  # a table laying the page out: its cells' content, in order
        grid = [[_cell(_one_line(self._inline_text(cell))) for cell in row] for row in rows]
        if not any(any(row) for row in grid):
            return []
        width = max(len(row) for row in grid)
        lines = [_table_row(row, width) for row in grid]
        lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
        blocks = self.blocks(caption) if caption is not None else []
        return [*blocks, "\n".join(lines)]

    def _rows(self, table: Element) -> Iterator[Element]:
        """The rows of a table, in its head, bodies, and foot, or directly in it; not those of nested tables."""
        for child, _ in self._children(table):
            if child is None:
                continue
            tag = tag_name(child)
            if tag == "tr":
                yield child
            elif tag in ("thead", "tbody", "tfoot"):
                yield from (row for row, _ in self._children(child) if row is not None and tag_name(row) == "tr")

    def _cells(self, row: Element) -> list[Element]:
        return [cell for cell, _ in self._children(row) if cell is not None and tag_name(cell) in ("td", "th")]

    def _inline(self, element: Element) -> str:
        """The inline Markdown of an element, its spaces collapsed but not trimmed."""
        tag = tag_name(element)
        if tag == "br":
            return _BREAK
        if tag == "a":
            return self._link(element)
        if tag == "img":
            return self._image(element)
        if tag in _CODE:
            return _code_span(_preformatted_text(element))
        content = self._inline_content(element)
        if tag in _EMPHASIS:
            return _wrapped(content.replace(_BREAK, " "), _EMPHASIS[tag])
        if tag in BLOCKS:  # a block inside inline content, such as a <div> inside a link
            return f" {content} "
        return content

    def _inline_content(self, element: Element) -> str:
        parts = [collapse_spaces(element.text)]
        for child, tail in self._children(element):
            if child is not None:
                parts.append(self._inline(child))
            parts.append(collapse_spaces(tail))
        return "".join(parts)

    def _inline_text(self, element: Element) -> str:
        return _finished(self._inline_content(element))

    def _link(self, element: Element) -> str:
        content = self._inline_content(element).replace(_BREAK, " ")
        url = self._url(element.get("href"), _LINKED_SCHEMES)
        if not content.strip() and url is not None:
            content = collapse_spaces(element.get("aria-label") or element.get("title")).strip()
        if url is None or not content.strip():
            return content
        return _wrapped(content, "", text=f"[{_bracketed(content.strip(' '))}]({_destination(url)})")

    def _image(self, element: Element) -> str:
        alt = collapse_spaces(element.get("alt")).strip()
        if not alt:
            return ""
        url = self._url(element.get("src"), _IMAGE_SCHEMES)
        return alt if url is None else f"![{_bracketed(alt)}]({_destination(url)})"

    def _url(self, href: str | None, schemes: frozenset[str]) -> str | None:
        if not href or not href.strip():
            return None
        try:
            url = self._urljoin(href)
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            return None
        return url if scheme in schemes else None


def _descendants(element: Element) -> Iterator[Element]:
    for node in element.iter():
        found = as_element(node)
        if found is not None and found is not element:
            yield found


def _lays_out(cell: Element) -> bool:
    return any(tag_name(node) in _LAYOUT for node in _descendants(cell))


def _add_paragraph(blocks: list[str], run: list[str]) -> None:
    text = _finished("".join(run))
    if text:
        blocks.append(text)
    run.clear()


def _finished(text: str) -> str:
    """Inline content with its spaces collapsed and trimmed, and its `<br>`s turned into line breaks."""
    return _SPACES_AROUND_BREAKS.sub("\n", _SPACES.sub(" ", text)).strip()


def _one_line(text: str) -> str:
    return " ".join(text.split("\n"))


def _wrapped(content: str, marker: str, *, text: str | None = None) -> str:
    """Content between markers, such as `**`, keeping the spaces around it outside them; `text` replaces it."""
    inner = content.strip(" ")
    if not inner:
        return content
    before = " " if content.startswith(" ") else ""
    after = " " if content.endswith(" ") else ""
    return f"{before}{marker}{inner if text is None else text}{marker}{after}"


def _bracketed(text: str) -> str:
    """Text as the text of a link, with the brackets that would end it escaped."""
    return text.replace("[", "\\[").replace("]", "\\]")


def _destination(url: str) -> str:
    """A URL as a link's destination, with the characters that would end it escaped."""
    return url.replace(" ", "%20").replace("(", "%28").replace(")", "%29").replace("<", "%3C").replace(">", "%3E")


def _code_span(text: str) -> str:
    code = collapse_spaces(text).strip()
    if not code:
        return ""
    fence = "`" * (_longest_run(code, "`") + 1)
    padding = " " if code.startswith("`") or code.endswith("`") else ""
    return f"{fence}{padding}{code}{padding}{fence}"


def _code_block(pre: Element) -> list[str]:
    code = _preformatted_text(pre).strip("\n").rstrip()
    if not code.strip():
        return []
    fence = "`" * max(3, _longest_run(code, "`") + 1)
    return [f"{fence}{_language(pre)}\n{code}\n{fence}"]


def _preformatted_text(element: Element) -> str:
    """The text of an element as it is written, with `<br>`s as line breaks."""
    parts = [element.text or ""]
    for child in element:
        node = as_element(child)
        if node is not None and tag_name(node) not in UNRENDERED and not is_hidden(node):
            parts.append("\n" if tag_name(node) == "br" else _preformatted_text(node))
        parts.append(child.tail or "")
    return "".join(parts)


def _language(pre: Element) -> str:
    """The language of a code block, from a `language-` or `lang-` class of the `<pre>` or of its `<code>`."""
    for element in (pre, *(node for node in map(as_element, pre) if node is not None and tag_name(node) == "code")):
        found = _LANGUAGE.search(element.get("class") or "")
        if found:
            return found.group(1)
    return ""


def _longest_run(text: str, character: str) -> int:
    return max((len(run) for run in re.findall(f"{re.escape(character)}+", text)), default=0)


def _indented(text: str, indent: str) -> str:
    """Text whose lines but the first are indented, as the content of a list item is."""
    first, *rest = text.split("\n")
    return "\n".join([first, *(indent + line if line else "" for line in rest)])


def _start(ordered_list: Element) -> int:
    start = (ordered_list.get("start") or "").strip()
    return int(start) if start.lstrip("-").isdigit() else 1


def _cell(text: str) -> str:
    return text.replace("|", "\\|")


def _table_row(cells: list[str], width: int) -> str:
    return "| " + " | ".join([*cells, *[""] * (width - len(cells))]) + " |"
