"""The visible text of HTML, laid out in lines as a browser's `innerText` lays it out."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pernix.extraction.elements import BLOCKS, UNRENDERED, as_element, collapse_spaces, is_hidden, tag_name

if TYPE_CHECKING:
    from pernix.extraction.elements import Element

__all__ = ["visible_text"]

_CELLS = frozenset({"td", "th"})
_PREFORMATTED = frozenset({"pre", "listing", "plaintext", "xmp"})


def visible_text(root: Element) -> str:
    """Return the text of an element as a page shows it.

    Scripts, styles, media, and hidden elements (see `is_hidden()`) are left out.
    Whitespace collapses as in HTML, but in `<pre>`. Block elements start and end a
    line, and paragraphs a blank one; `<br>` breaks a line, and table cells are
    separated by tabs.

    Args:
        root: The element, such as a document's `<html>`.

    Returns:
        The text, without blank lines around it.
    """
    layout = _Layout()
    layout.add(root, preformatted=False)
    return layout.text()


class _Layout:
    """Lays text out in lines, joining what collapses: spaces in a row, and line breaks blocks ask for."""

    def __init__(self) -> None:
        self._parts: list[str] = []
        self._breaks = 0  # line breaks the blocks around ask for before the next text
        self._space = False  # a collapsed space waiting for the next text on the line
        self._newlines = 0  # line breaks the text written so far ends with

    def add(self, element: Element, *, preformatted: bool) -> None:
        tag = tag_name(element)
        if tag in UNRENDERED or is_hidden(element):
            return
        if tag == "br":
            self._write("\n")
            return
        breaks = 2 if tag == "p" else 1 if tag in BLOCKS and tag not in _CELLS else 0
        self._break(breaks)
        preformatted = preformatted or tag in _PREFORMATTED
        self._text(element.text, preformatted=preformatted)
        cells = 0
        for child in element:
            node = as_element(child)
            if node is not None:
                if tag == "tr" and tag_name(node) in _CELLS:
                    if cells and not self._breaks:  # a line break that a cell's content ends with separates it too
                        self._write("\t")
                    cells += 1
                self.add(node, preformatted=preformatted)
            self._text(child.tail, preformatted=preformatted)
        self._break(breaks)

    def text(self) -> str:
        return "".join(self._parts).strip("\n")

    def _text(self, text: str | None, *, preformatted: bool) -> None:
        if not text:
            return
        if preformatted:
            self._write(text)
            return
        text = collapse_spaces(text)
        words = text.strip(" ")
        if text.startswith(" "):
            self._space = True
        if words:
            self._write(words)
            self._space = text.endswith(" ")

    def _break(self, count: int) -> None:
        self._breaks = max(self._breaks, count)

    def _write(self, text: str) -> None:
        """Write text as it is, after the line breaks, or the space, waiting before it."""
        if self._parts:
            if self._breaks > self._newlines:
                self._append("\n" * (self._breaks - self._newlines))
            elif self._space and not self._breaks and not self._newlines:
                self._append(" ")
        self._breaks = 0
        self._space = False
        self._append(text)

    def _append(self, text: str) -> None:
        self._parts.append(text)
        kept = text.rstrip("\n")
        self._newlines = len(text) - len(kept) + (self._newlines if not kept else 0)
