"""Render options, their actions, and the resource types they block, without a browser."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from pernix import Request
from pernix.browser import RESOURCE_TYPES, Click, Evaluate, PageMethod, Render, Screenshot, Scroll, Type, WaitFor
from pernix.browser.url_globs import glob_pattern
from pernix.errors import SettingsError
from pernix.fetchers import BrowserSettings
from pernix.fetchers.page_traffic import ResourceBlocker
from pernix.settings import CORE_SECTIONS, load_settings

if TYPE_CHECKING:
    from pathlib import Path


def test_render_takes_sequences_and_single_values_and_keeps_tuples() -> None:
    render = Render(
        wait_for="div.item",
        actions=[Scroll(times=3), Click("button.more")],
        block=["image", "font", "image"],
        capture="**/api/list*",
    )

    assert render.actions == (Scroll(times=3), Click("button.more"))
    assert render.block == ("image", "font")
    assert render.capture == ("**/api/list*",)
    assert Render(block="media").block == ("media",)
    assert Render() == Render(wait_for=None, actions=(), block=None, capture=(), screenshot=False)
    assert hash(render) == hash(
        Render(wait_for="div.item", actions=render.actions, block=render.block, capture=render.capture)
    )


def test_page_methods_are_kept_as_page_methods() -> None:
    async def log_in(page: object) -> None:
        pass

    marked = PageMethod(log_in, optional=True, timeout=5)
    render = Render(setup=log_in, action=marked, keep_page=True)

    assert render.setup == PageMethod(log_in)
    assert render.action is marked
    assert render.keep_page
    assert (Render().setup, Render().action, Render().keep_page) == (None, None, False)
    assert render == Render(setup=PageMethod(log_in), action=marked, keep_page=True)


def test_page_methods_must_be_async() -> None:
    def close_banner(page: object) -> None:
        pass

    with pytest.raises(TypeError, match=r"PageMethod: .* is not an async method"):
        Render(action=close_banner)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize("timeout", [0, -1.0])
def test_a_page_method_timeout_must_be_positive(timeout: float) -> None:
    async def close_banner(page: object) -> None:
        pass

    with pytest.raises(ValueError, match="timeout must be positive"):
        PageMethod(close_banner, timeout=timeout)


def test_block_none_defers_to_the_setting_and_empty_blocks_nothing() -> None:
    assert Render().block is None
    assert Render(block=()).block == ()


def test_the_blockable_resource_types_are_the_browsers() -> None:
    expected = {
        "image",
        "media",
        "font",
        "stylesheet",
        "script",
        "xhr",
        "fetch",
        "eventsource",
        "texttrack",
        "manifest",
    }

    assert expected | {"other"} == RESOURCE_TYPES


@pytest.mark.parametrize(
    ("block", "message"),
    [
        (("imgae",), r"Render\(block=...\): 'imgae' is not a resource type; did you mean 'image'\?"),
        (("stylesheets",), "did you mean 'stylesheet'"),
        (("pictures",), "'pictures' is not a resource type; the types are eventsource, fetch, font, image"),
        (("document",), "'document' cannot be blocked: the page itself is a document"),
        (("websocket",), "'websocket' cannot be blocked: the browser does not let a page's WebSockets be blocked"),
    ],
)
def test_types_that_cannot_be_blocked_are_refused(block: tuple[str, ...], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        Render(block=block)  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("options", "error", "message"),
    [
        ({"wait_for": ""}, ValueError, "wait_for must be a CSS selector"),
        ({"capture": ["**/api/*", ""]}, ValueError, "a capture pattern must be a URL glob"),
        ({"capture": "**/{a,b"}, ValueError, "opens a brace it does not close"),
        ({"actions": ["click"]}, TypeError, "'click' is not an action"),
        ({"block": [1]}, TypeError, "a resource type is a string"),
    ],
)
def test_invalid_render_options_are_refused(options: dict[str, Any], error: type[Exception], message: str) -> None:
    with pytest.raises(error, match=message):
        Render(**options)


def test_actions_take_their_first_field_by_position_and_the_rest_by_keyword() -> None:
    assert Click("a.more", timeout=2, optional=True) == Click(selector="a.more", timeout=2, optional=True)
    assert Type("input[name=q]", "dune", delay=0.05).text == "dune"
    assert WaitFor("div.loaded").state == "visible"
    assert Screenshot().full_page is True
    assert Evaluate("document.title").isolated is False
    assert repr(Scroll(times=3)) == "Scroll(times=3)"


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: Click(""), "Click: selector must be a CSS selector"),
        (lambda: WaitFor("div", state="gone"), "WaitFor: state must be one of"),  # pyright: ignore[reportArgumentType]
        (lambda: Scroll(times=0), "Scroll: times must be a positive integer"),
        (lambda: Scroll(times=True), "Scroll: times must be a positive integer"),
        (lambda: Scroll(delay=-1), "Scroll: delay must not be negative"),
        (lambda: Type("input", "x", delay=-0.1), "Type: delay must not be negative"),
        (lambda: Evaluate("  "), "Evaluate: script must be JavaScript"),
        (lambda: Screenshot(timeout=0), "Screenshot: timeout must be positive"),
    ],
)
def test_invalid_actions_are_refused(make: Any, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make()


def test_a_request_renders_with_true_or_a_render_and_nothing_else() -> None:
    render = Render(actions=[Click("button")])

    assert Request("https://shop.example/", render=render).render is render
    assert Request("https://shop.example/").replace(render=render).render is render
    assert Request("https://shop.example/", render=True).render is True
    with pytest.raises(TypeError, match=r"render must be True, False, or a pernix\.browser\.Render"):
        Request("https://shop.example/", render={"wait_for": "div"})  # pyright: ignore[reportArgumentType]


@pytest.mark.parametrize(
    ("glob", "matches", "misses"),
    [
        (
            "**/api/list*",
            ["https://shop.example/api/list?page=2", "http://a/b/api/list"],
            ["https://shop.example/api/lists/x"],
        ),
        ("https://shop.example/*", ["https://shop.example/books"], ["https://shop.example/books/1"]),
        ("**/*.{png,jpg}", ["https://cdn.example/a/b.png", "https://cdn.example/c.jpg"], ["https://cdn.example/c.gif"]),
        ("**/api/**/items", ["https://x/api/items", "https://x/api/v1/v2/items"], ["https://x/api-items"]),
        ("https://x/search?q=*", ["https://x/search?q=dune"], ["https://x/searchXq=dune"]),
        (r"https://x/\*", ["https://x/*"], ["https://x/a"]),
    ],
)
def test_capture_globs_read_as_the_browsers_route_patterns(glob: str, matches: list[str], misses: list[str]) -> None:
    pattern = glob_pattern(glob)

    assert [bool(pattern.fullmatch(url)) for url in matches] == [True] * len(matches)
    assert [bool(pattern.fullmatch(url)) for url in misses] == [False] * len(misses)


def test_blocking_other_covers_the_types_blocking_does_not_name() -> None:
    blocker = ResourceBlocker(["image", "other"])

    assert [blocker.blocks(kind) for kind in ("image", "ping", "other", "script", "document")] == [
        True,
        True,
        True,
        False,
        False,
    ]
    assert not ResourceBlocker(["image"]).blocks("ping")


def test_browser_block_is_empty_by_default_and_checked(tmp_path: Path) -> None:
    def browser_settings(**overrides: object) -> BrowserSettings:
        settings = load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ={}, overrides=overrides)
        return settings.section(BrowserSettings)

    assert browser_settings().block == ()
    assert browser_settings().action_timeout == 10.0
    assert browser_settings(**{"browser.block": ["image", "font"]}).block == ("image", "font")
    with pytest.raises(SettingsError, match=r"browser\.block: 'fnot' is not a resource type; did you mean 'font'\?"):
        browser_settings(**{"browser.block": ["fnot"]})
    with pytest.raises(SettingsError, match=r"browser\.block: 'document' cannot be blocked"):
        browser_settings(**{"browser.block": ["document"]})
    with pytest.raises(SettingsError, match=r"browser\.action_timeout"):
        browser_settings(**{"browser.action_timeout": 0})


def test_browser_block_comes_from_the_environment(tmp_path: Path) -> None:
    environ = {"PERNIX_BROWSER__BLOCK": '["image", "media"]'}

    settings = load_settings(sections=CORE_SECTIONS, project_dir=tmp_path, environ=environ)

    assert settings.section(BrowserSettings).block == ("image", "media")
