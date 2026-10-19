"""The Pinned Pair: the bundled browser build, as it names itself, and the pinned Profile."""

from __future__ import annotations

import importlib.resources
import json
import sys

import pytest
from curl_cffi.requests import BrowserType

from pernix.fetchers import DEFAULT_PROFILE
from pernix.fetchers.pinned_pair import (
    BUNDLED_BROWSER,
    PINNED_PROFILE,
    accept_language,
    client_headers,
    running_platform,
)

UA_MACOS = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/151.0.0.0 Safari/537.36"
)


def test_the_bundled_build_is_the_one_the_pinned_patchright_installs() -> None:
    listing = importlib.resources.files("patchright") / "driver" / "package" / "browsers.json"
    browsers = json.loads(listing.read_text())["browsers"]

    [chromium] = [browser for browser in browsers if browser["name"] == "chromium"]
    assert chromium["browserVersion"] == BUNDLED_BROWSER.version


def test_the_pinned_profile_is_a_curl_cffi_target_and_the_default() -> None:
    assert PINNED_PROFILE in {browser.value for browser in BrowserType}
    assert DEFAULT_PROFILE == PINNED_PROFILE


@pytest.mark.parametrize(
    ("platform", "user_agent"),
    [
        ("macOS", UA_MACOS),
        (
            "Linux",
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36",
        ),
        (
            "Windows",
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/151.0.0.0 Safari/537.36",
        ),
    ],
)
def test_the_user_agent_is_chromes_reduced_one_for_the_platform(platform: str, user_agent: str) -> None:
    assert BUNDLED_BROWSER.user_agent(platform) == user_agent


def test_the_brands_are_those_chrome_for_testing_names() -> None:
    assert BUNDLED_BROWSER.major == "151"
    assert BUNDLED_BROWSER.sec_ch_ua() == '"Chromium";v="151", "Not=A?Brand";v="99"'


@pytest.mark.parametrize(
    ("languages", "header"),
    [
        (["en-US", "en"], "en-US,en;q=0.9"),
        (["en-US"], "en-US,en;q=0.9"),
        (["de"], "de"),
        (["de-DE", "de", "en-US", "en"], "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"),
        (["en-GB", "en-US", "en"], "en-GB,en-US;q=0.9,en;q=0.8"),
        (["en-US", "fr", "en"], "en-US,en;q=0.9,fr;q=0.8"),
        (
            list("abcdefghijkl"),
            "a,b;q=0.9,c;q=0.8,d;q=0.7,e;q=0.6,f;q=0.5,g;q=0.4,h;q=0.3,i;q=0.2,j;q=0.1,k;q=0.1,l;q=0.1",
        ),
    ],
    ids=["en-US,en", "en-US", "de", "de-DE...", "en-GB...", "en-US,fr,en", "twelve"],
)
def test_the_accept_language_is_the_one_chrome_makes(languages: list[str], header: str) -> None:
    assert accept_language(languages) == header


def test_the_client_headers_come_in_chromes_order_cased_as_curl_sends_them() -> None:
    assert client_headers(["en-US", "en"], platform="macOS") == [
        ("sec-ch-ua", '"Chromium";v="151", "Not=A?Brand";v="99"'),
        ("sec-ch-ua-mobile", "?0"),
        ("sec-ch-ua-platform", '"macOS"'),
        ("User-Agent", UA_MACOS),
        ("Accept-Language", "en-US,en;q=0.9"),
    ]


@pytest.mark.parametrize(
    ("system", "platform"), [("darwin", "macOS"), ("win32", "Windows"), ("linux", "Linux"), ("freebsd14", "Linux")]
)
def test_the_running_platform_is_named_as_chrome_names_it(
    monkeypatch: pytest.MonkeyPatch, system: str, platform: str
) -> None:
    monkeypatch.setattr(sys, "platform", system)

    assert running_platform() == platform
