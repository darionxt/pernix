"""Reading HTML documents as people see them: their visible text, their main content as Markdown, and their links.

It works on the lxml trees parsel Selectors hold, such as `Response.selector.root`,
for `pernix fetch`.
"""

from pernix.extraction.elements import Element, as_element
from pernix.extraction.links import Link, page_links
from pernix.extraction.main_content import MainContent, main_content
from pernix.extraction.markdown import main_markdown, markdown
from pernix.extraction.visible_text import visible_text

__all__ = [
    "Element",
    "Link",
    "MainContent",
    "as_element",
    "main_content",
    "main_markdown",
    "markdown",
    "page_links",
    "visible_text",
]
