"""The `proxies.*` settings."""

from __future__ import annotations

import pytest

from pernix.errors import SettingsError
from pernix.secret import Secret
from pernix.settings import ProxySettings, load_settings


def test_proxies_are_off_until_a_source_is_set() -> None:
    assert not ProxySettings().enabled
    assert ProxySettings(urls=("proxy.example:8080",)).enabled
    assert ProxySettings(api=Secret("https://vendor.example/proxies?key=k")).enabled


def test_the_api_is_a_secret_as_its_url_often_holds_a_key() -> None:
    settings = load_settings(spider_settings={"proxies.api": "https://vendor.example/proxies?key=k"})

    assert settings.section(ProxySettings).api == Secret("https://vendor.example/proxies?key=k")


def test_listed_proxies_that_are_not_valid_are_named_without_quoting_them() -> None:
    with pytest.raises(SettingsError) as refused:
        load_settings(spider_settings={"proxies.urls": ["proxy.example:8080", "user:secret-pw@proxy.example"]})

    assert "proxies.urls[1]: a proxy needs a port" in str(refused.value)
    assert "secret-pw" not in str(refused.value)
