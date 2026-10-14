from __future__ import annotations

import pytest
from curl_cffi.requests import BrowserType

from pernix.errors import SettingsError
from pernix.fetchers import DEFAULT_PROFILE, HTTPSettings
from pernix.fetchers.http_settings import check_profile
from pernix.settings import load_settings


def test_the_default_profile_is_a_pinned_chrome() -> None:
    assert DEFAULT_PROFILE in {browser.value for browser in BrowserType}
    assert DEFAULT_PROFILE.startswith("chrome")
    assert HTTPSettings().impersonate == DEFAULT_PROFILE


@pytest.mark.parametrize(
    ("profile", "problem"),
    [
        ("chrome", r"the Profile 'chrome' drifts as curl-cffi is upgraded; pin a version, such as 'chrome150'"),
        ("safari", r"the Profile 'safari' drifts"),
        ("chrome1500", r"unknown Profile 'chrome1500'; did you mean 'chrome150'\?"),
    ],
)
def test_profiles_must_be_pinned_targets(profile: str, problem: str) -> None:
    with pytest.raises(ValueError, match=problem):
        check_profile(profile)


def test_the_settings_check_the_profile_and_the_limits() -> None:
    with pytest.raises(SettingsError, match=r"http\.impersonate: the Profile 'chrome' drifts"):
        load_settings(overrides={"http.impersonate": "chrome"})
    with pytest.raises(SettingsError) as raised:
        load_settings(overrides={"http.timeout": 0, "http.max_redirects": -1})

    assert [problem.partition(":")[0] for problem in raised.value.problems] == ["http.timeout", "http.max_redirects"]


def test_the_http_fetcher_registers_the_settings() -> None:
    settings = load_settings(overrides={"http.impersonate": "chrome136", "http.max_body_size": 0})

    assert settings.section(HTTPSettings) == HTTPSettings(impersonate="chrome136", max_body_size=0)
