"""`pernix fetch` reading pages for agents: text, Markdown, selected parts, links, and a size limit."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import orjson
import pytest

from pernix.cli import main

if TYPE_CHECKING:
    from pathlib import Path

    from pernix.testing import LoopbackServer

PAGE = """<!doctype html>
<html><head><title>Dune</title><script>var tracked = 1;</script><style>h1 { color: red }</style></head>
<body>
<header><nav><a href="/">Home</a> <a href="/books">Books</a></nav></header>
<main>
<h1>Dune</h1>
<p>A novel by <a href="/authors/herbert">Frank Herbert</a>.</p>
<h2>Editions</h2>
<table><tr><th>Year</th><th>Publisher</th></tr><tr><td>1965</td><td>Chilton</td></tr></table>
<ul><li>Hardcover</li><li>Paperback</li></ul>
</main>
<footer>Copyright Books Inc. <a href="/privacy">Privacy</a></footer>
</body></html>
"""

MARKDOWN = (
    "# Dune\n\n"
    "A novel by [Frank Herbert]({base}/authors/herbert).\n\n"
    "## Editions\n\n"
    "| Year | Publisher |\n| --- | --- |\n| 1965 | Chilton |\n\n"
    "- Hardcover\n- Paperback"
)

_RENDERED = (
    "<main><h1 id=title>Served</h1><ul id=list></ul></main><script>"
    "document.getElementById('title').textContent = 'Rendered';"
    "document.getElementById('list').innerHTML = '<li><a href=\"/one\">One</a></li><li>Two</li>';"
    "</script>"
)


@pytest.fixture
def site(server: LoopbackServer) -> LoopbackServer:
    server.route("/dune", PAGE, headers=[("Content-Type", "text/html; charset=utf-8")])
    server.route("/data", '{"title": "Dune"}', headers=[("Content-Type", "application/json")])
    server.route("/logo", b"\x89PNG\x00\x01", headers=[("Content-Type", "image/png")])
    server.route("/rendered", _RENDERED, headers=[("Content-Type", "text/html")])
    return server


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    return orjson.loads(capsys.readouterr().out)


def test_markdown_is_the_main_content_with_absolute_links(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["fetch", site.url("/dune"), "--extract", "markdown"]) == 0
    out, err = capsys.readouterr()
    assert out == MARKDOWN.format(base=site.base_url) + "\n"
    assert err.startswith(f"HTTP/1.1 200 {site.url('/dune')} (")

    assert main(["fetch", site.url("/dune"), "--extract", "markdown", "--json"]) == 0
    described = _json(capsys)
    assert (described["body"], described["truncated"]) == (MARKDOWN.format(base=site.base_url), False)


def test_text_is_the_visible_text_of_the_whole_page(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", site.url("/dune"), "--extract", "text", "--json"]) == 0

    body = _json(capsys)["body"]
    assert body == (
        "Home Books\nDune\n\nA novel by Frank Herbert.\n\nEditions\nYear\tPublisher\n1965\tChilton\n"
        "Hardcover\nPaperback\nCopyright Books Inc. Privacy"
    )


def test_selectors_pick_parts_of_the_page_in_order(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["fetch", site.url("/dune"), "--css", "h2", "--xpath", "//li/text()", "--css", "h1::text", "--json"]) == 0
    )
    described = _json(capsys)
    assert (described["body"], described["matches"]) == (None, ["<h2>Editions</h2>", "Hardcover", "Paperback", "Dune"])

    assert main(["fetch", site.url("/dune"), "--css", "table", "--css", "main p", "--extract", "markdown"]) == 0
    assert capsys.readouterr().out == (
        "| Year | Publisher |\n| --- | --- |\n| 1965 | Chilton |\n"
        f"A novel by [Frank Herbert]({site.base_url}/authors/herbert).\n"
    )

    assert main(["fetch", site.url("/dune"), "--css", "main p", "--extract", "text", "--json"]) == 0
    assert _json(capsys)["matches"] == ["A novel by Frank Herbert."]


def test_links_are_listed_as_absolute_urls_with_their_text(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str]
) -> None:
    base = site.base_url

    assert main(["fetch", site.url("/dune"), "--links", "--json"]) == 0
    described = _json(capsys)
    assert described["body"] is None
    assert described["links"] == [
        {"url": f"{base}/", "text": "Home"},
        {"url": f"{base}/books", "text": "Books"},
        {"url": f"{base}/authors/herbert", "text": "Frank Herbert"},
        {"url": f"{base}/privacy", "text": "Privacy"},
    ]

    assert main(["fetch", site.url("/dune"), "--links", "--css", "main"]) == 0
    assert capsys.readouterr().out == f"{base}/authors/herbert\tFrank Herbert\n"


def test_max_chars_cuts_what_is_read(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", site.url("/dune"), "--extract", "markdown", "--max-chars", "6", "--json"]) == 0
    assert (_json(capsys)["body"], True) == ("# Dune", True)

    assert (
        main(["fetch", site.url("/dune"), "--css", "li::text", "--css", "h1::text", "--max-chars", "12", "--json"]) == 0
    )
    described = _json(capsys)
    assert (described["matches"], described["truncated"]) == (["Hardcover", "Pap"], True)

    assert main(["fetch", site.url("/dune"), "--links", "--max-chars", "40", "--json"]) == 0
    described = _json(capsys)
    assert (len(described["links"]), described["truncated"]) == (1, True)

    assert main(["fetch", site.url("/dune"), "--max-chars", "15"]) == 0
    out, err = capsys.readouterr()
    assert out == "<!doctype html>"
    assert err.endswith("Cut at 15 characters (--max-chars)\n")

    assert main(["fetch", site.url("/dune"), "--max-chars", "100000", "--json"]) == 0
    assert _json(capsys)["truncated"] is False


def test_bodies_other_than_html_are_read_as_they_are(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["fetch", site.url("/data"), "--extract", "markdown", "--json"]) == 0
    assert _json(capsys)["body"] == '{"title": "Dune"}'

    assert main(["fetch", site.url("/data"), "--css", "h1", "--json"]) == 0
    assert _json(capsys)["matches"] == []

    assert main(["fetch", site.url("/logo"), "--extract", "text", "--json"]) == 0
    assert _json(capsys)["body"] is None


def test_the_body_saved_with_o_is_not_read_but_selectors_are(
    site: LoopbackServer, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "dune.html"

    assert main(["fetch", site.url("/dune"), "-o", str(target), "--css", "h1::text", "--json"]) == 0

    assert target.read_text() == PAGE
    described = _json(capsys)
    assert (described["body"], described["matches"]) == (None, ["Dune"])


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--css", "h1["], "--css 'h1[' is not valid: Expected ident or '*', got <EOF at 3>"),
        (["--css", "::nothing"], "--css '::nothing' is not valid: The pseudo-element ::nothing is unknown"),
        (["--xpath", "//a["], "--xpath '//a[' is not valid: XPath error: Invalid expression in //a["),
    ],
)
def test_invalid_selectors_exit_with_2_before_fetching(
    site: LoopbackServer, capsys: pytest.CaptureFixture[str], argv: list[str], problem: str
) -> None:
    assert main(["fetch", site.url("/dune"), *argv]) == 2

    assert capsys.readouterr().err == f"Error: {problem}\n"
    assert site.received == []


@pytest.mark.parametrize(
    ("argv", "problem"),
    [
        (["--max-chars", "0"], "argument --max-chars: expected a positive number, got '0'"),
        (["--max-chars", "1.5"], "argument --max-chars: expected a positive number, got '1.5'"),
        (["--timeout", "-1"], "argument --timeout: expected a positive number, got '-1'"),
        (["--extract", "html"], "argument --extract: invalid choice: 'html'"),
    ],
)
def test_invalid_option_values_exit_with_2(capsys: pytest.CaptureFixture[str], argv: list[str], problem: str) -> None:
    with pytest.raises(SystemExit) as exited:
        main(["fetch", "https://example.com/", *argv])

    assert exited.value.code == 2
    assert problem in capsys.readouterr().err


@pytest.mark.usefixtures("browser_engine")
def test_reading_applies_to_the_rendered_page(site: LoopbackServer, capsys: pytest.CaptureFixture[str]) -> None:
    url = site.url("/rendered")

    assert main(["fetch", url, "--fetcher", "browser", "--extract", "markdown", "--json"]) == 0
    described = _json(capsys)
    assert (described["fetcher"], described["body"]) == (
        "browser",
        f"# Rendered\n\n- [One]({site.base_url}/one)\n- Two",
    )

    assert main(["fetch", url, "--fetcher", "browser", "--css", "li", "--extract", "text", "--json"]) == 0
    assert _json(capsys)["matches"] == ["One", "Two"]

    assert main(["fetch", url, "--fetcher", "browser", "--links"]) == 0
    assert capsys.readouterr().out == f"{site.base_url}/one\tOne\n"
