"""Reading HTML as people see it: visible text, the main content as Markdown, and links."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from pernix.extraction import Link, as_element, main_content, main_markdown, markdown, page_links, visible_text
from pernix.testing import make_response

if TYPE_CHECKING:
    from pernix.extraction import Element
    from pernix.response import Response

URL = "https://books.example/dune/"

ARTICLE = """
<html><head><title>Dune</title><style>p { color: red }</style><script>var tracked = 1;</script></head>
<body>
<header><nav><a href="/">Home</a> <a href="/books">Books</a></nav></header>
<aside><h3>Related</h3><a href="/x">X</a></aside>
<main>
<h1>Dune</h1>
<p>A novel by <a href="/authors/herbert">Frank Herbert</a>, set on <em>Arrakis</em>.</p>
<h2>Editions</h2>
<table><thead><tr><th>Year</th><th>Publisher</th></tr></thead>
<tbody><tr><td>1965</td><td>Chilton</td></tr><tr><td>2005</td><td>Ace</td></tr></tbody></table>
<ul><li>Hardcover</li><li>Paperback<ol><li>first</li><li>second</li></ol></li></ul>
<pre class="language-python"><code>print("spice")
for i in range(3):
    print(i)</code></pre>
<p hidden>Hidden</p><p style="color: red; display:none">Not shown</p>
<div class="share-buttons"><a href="/share">Share</a></div>
</main>
<footer><p>Copyright Books Inc. <a href="/privacy">Privacy</a></p></footer>
</body></html>
"""


def _response(body: str, url: str = URL) -> Response:
    return make_response(url, body)


def _root(response: Response) -> Element:
    root = as_element(response.selector.root)
    assert root is not None
    return root


def _markdown(body: str) -> str:
    response = _response(body)
    return main_markdown(_root(response), urljoin=response.urljoin)


def _text(body: str) -> str:
    return visible_text(_root(_response(body)))


def test_visible_text_leaves_out_scripts_styles_and_hidden_elements() -> None:
    text = _text(ARTICLE)

    assert "tracked" not in text
    assert "color" not in text
    assert "Hidden" not in text
    assert "Not shown" not in text
    assert "A novel by Frank Herbert, set on Arrakis." in text
    assert "Copyright Books Inc. Privacy" in text


def test_visible_text_is_laid_out_in_lines_as_a_page_shows_it() -> None:
    text = _text(
        "<h1>Title</h1><p>One   two\n three</p><p>Four<br>five</p><div>Six <span>seven</span></div>"
        "<table><tr><td>a</td><td>b</td></tr><tr><td>c</td><td>d</td></tr></table>"
        "<pre>  kept\n    as is</pre>"
    )

    assert text == "Title\n\nOne two three\n\nFour\nfive\n\nSix seven\na\tb\nc\td\n  kept\n    as is"


def test_markdown_keeps_the_main_content_and_drops_the_boilerplate() -> None:
    assert _markdown(ARTICLE) == (
        "# Dune\n\n"
        "A novel by [Frank Herbert](https://books.example/authors/herbert), set on *Arrakis*.\n\n"
        "## Editions\n\n"
        "| Year | Publisher |\n| --- | --- |\n| 1965 | Chilton |\n| 2005 | Ace |\n\n"
        "- Hardcover\n- Paperback\n  1. first\n  2. second\n\n"
        '```python\nprint("spice")\nfor i in range(3):\n    print(i)\n```'
    )


def test_links_and_images_are_made_absolute_against_the_base_url() -> None:
    body = (
        '<html><head><base href="https://cdn.example/assets/"></head><body><article>'
        '<p><a href="guide.html">the guide</a> <img src="map.png" alt="A map"> <img src="spacer.gif">'
        '<a href="javascript:void(0)">no link</a> <a href="mailto:me@example.com">mail</a></p>'
        "</article></body></html>"
    )

    assert _markdown(body) == (
        "[the guide](https://cdn.example/assets/guide.html) ![A map](https://cdn.example/assets/map.png) "
        "no link [mail](mailto:me@example.com)"
    )


def test_without_landmarks_the_body_is_read_without_its_header_footer_and_menus() -> None:
    body = """
    <body>
    <header><a href="/">Logo</a></header>
    <div class="cookie-banner">We use cookies</div>
    <div class="breadcrumbs-x9"><a href="/">Home</a> <a href="/docs">Docs</a> <a href="/docs/a">A</a></div>
    <div class="content-sidebar-wrap">
      <h1>Install</h1>
      <p>Run the installer, then restart.</p>
      <section><header><h2>Notes</h2></header><p>It takes a minute.</p></section>
    </div>
    <div id="sidebar"><p>Popular posts</p></div>
    <footer>Copyright</footer>
    </body>
    """

    assert _markdown(body) == "# Install\n\nRun the installer, then restart.\n\n## Notes\n\nIt takes a minute."


def test_a_page_made_of_links_keeps_them() -> None:
    body = '<body><ul><li><a href="/a">A</a></li><li><a href="/b">B</a></li><li><a href="/c">C</a></li></ul></body>'

    assert _markdown(body) == (
        "- [A](https://books.example/a)\n- [B](https://books.example/b)\n- [C](https://books.example/c)"
    )


def test_the_only_article_is_the_main_content_but_several_are_not() -> None:
    one = _root(_response("<body><p>Intro</p><article><p>Story</p></article></body>"))
    several = _root(_response("<body><article><p>One</p></article><article><p>Two</p></article></body>"))

    assert main_content(one).root.tag == "article"
    assert main_content(several).root.tag == "body"


@pytest.mark.parametrize(
    ("html", "expected"),
    [
        ("<blockquote><p>Quoted</p><p>Twice</p></blockquote>", "> Quoted\n>\n> Twice"),
        ('<ol start="3"><li>three</li><li>four</li></ol>', "3. three\n4. four"),
        ("<p>Use <code>a `b` c</code> here</p>", "Use ``a `b` c`` here"),
        ("<p><strong> bold </strong>and<i>italic</i></p>", "**bold** and*italic*"),
        ("<p>First<br>second</p>", "First\nsecond"),
        ("<hr><dl><dt>Term</dt><dd>Meaning</dd></dl>", "---\n\n**Term**\n\nMeaning"),
        ("<pre><code>a ```fence``` b</code></pre>", "````\na ```fence``` b\n````"),
        ('<p><a href="/x">[1]</a></p>', "[\\[1\\]](https://books.example/x)"),
        ('<p><a href="/a b(c)">spaced</a></p>', "[spaced](https://books.example/a%20b%28c%29)"),
        (
            '<p><a href="/icon" aria-label="Settings"><img src="cog.svg"></a></p>',
            "[Settings](https://books.example/icon)",
        ),
        ("<table><tr><th>a|b</th><th>c</th></tr><tr><td>1</td></tr></table>", "| a\\|b | c |\n| --- | --- |\n| 1 |  |"),
        ("<table><tr><td><p>Layout</p></td><td>cell</td></tr></table>", "Layout\n\ncell"),
        ('<a href="/card"><h3>Card</h3><p>Text</p></a>', "[Card Text](https://books.example/card)"),
        ("<span><div>A span holding a div</div></span>", "A span holding a div"),
        ("<p>Fill <input value='x'> <button>Send</button> <select><option>A</option></select>in</p>", "Fill in"),
    ],
)
def test_markdown_of_html(html: str, expected: str) -> None:
    response = _response(f"<html><body>{html}</body></html>")

    assert markdown(_root(response), urljoin=response.urljoin) == expected


def test_markdown_of_a_selected_element_is_the_element_itself() -> None:
    response = _response(ARTICLE)

    table = as_element(response.css("table")[0].root)
    link = as_element(response.css("main p a")[0].root)
    assert table is not None
    assert link is not None
    assert markdown(table, urljoin=response.urljoin).startswith("| Year | Publisher |\n| --- | --- |")
    assert markdown(link, urljoin=response.urljoin) == "[Frank Herbert](https://books.example/authors/herbert)"


def test_links_are_absolute_and_listed_once_with_their_text() -> None:
    response = _response(
        '<html><body><a href="/a"><img src="a.png" alt="Logo"></a><a href="/b"></a><a href="/b">Bee</a>'
        '<a href="/a">Again</a><a href="ftp://files.example/">FTP</a><a>none</a><area href="/map" alt="Map">'
        '<a href="/c" title="Sea">  </a></body></html>'
    )

    assert page_links(_root(response), urljoin=response.urljoin) == [
        Link("https://books.example/a", "Logo"),
        Link("https://books.example/b", "Bee"),
        Link("https://books.example/map", "Map"),
        Link("https://books.example/c", "Sea"),
    ]
