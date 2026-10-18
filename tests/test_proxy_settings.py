"""The `proxies.*` settings."""

from __future__ import annotations

import pytest

from pernix.errors import SettingsError
from pernix.proxies import Proxy
from pernix.secret import Secret
from pernix.settings import ProxySettings, load_settings


def test_proxies_are_off_until_a_source_or_a_binding_policy_is_set() -> None:
    assert not ProxySettings().enabled
    assert ProxySettings(urls=("proxy.example:8080",)).enabled
    assert ProxySettings(api=Secret("https://vendor.example/proxies?key=k")).enabled
    assert ProxySettings(binding="myproject.proxies.Sticky").enabled  # giving Sessions proxies of their own
    assert not ProxySettings(binding="myproject.proxies.Sticky").sourced


def test_the_api_is_a_secret_as_its_url_often_holds_a_key() -> None:
    settings = load_settings(spider_settings={"proxies.api": "https://vendor.example/proxies?key=k"})

    assert settings.section(ProxySettings).api == Secret("https://vendor.example/proxies?key=k")


def test_listed_proxies_that_are_not_valid_are_named_without_quoting_them() -> None:
    with pytest.raises(SettingsError) as refused:
        load_settings(spider_settings={"proxies.urls": ["proxy.example:8080", "user:secret-pw@proxy.example"]})

    assert "proxies.urls[1]: a proxy needs a port" in str(refused.value)
    assert "secret-pw" not in str(refused.value)


def test_listed_proxies_may_be_tables_to_tag_them() -> None:
    settings = load_settings(
        spider_settings={"proxies.urls": ["a.example:1", {"url": "b.example:2", "tags": ["us", "residential"]}]}
    )

    assert settings.section(ProxySettings).urls == (
        "a.example:1",
        Proxy("b.example:2", tags=frozenset({"us", "residential"})),
    )


def test_a_listed_table_must_be_a_proxy_record() -> None:
    with pytest.raises(SettingsError, match="unknown field `tag`"):
        load_settings(spider_settings={"proxies.urls": [{"url": "b.example:2", "tag": ["us"]}]})
    with pytest.raises(SettingsError) as refused:
        load_settings(spider_settings={"proxies.urls": [{"url": "user:secret-pw@b.example"}]})

    assert "proxies.urls[0]: a proxy needs a port" in str(refused.value)
    assert "secret-pw" not in str(refused.value)


def test_sessions_keep_their_proxy_with_no_limit_on_how_many_share_one_unless_set() -> None:
    defaults = ProxySettings()

    assert (defaults.max_sessions, defaults.rebind, defaults.binding) == (None, "unavailable", None)
    for rebind in ("every_lease", "retired", "never"):
        assert load_settings(spider_settings={"proxies.rebind": rebind}).section(ProxySettings).rebind == rebind
    for wrong in ({"proxies.max_sessions": 0}, {"proxies.rebind": "always"}):
        with pytest.raises(SettingsError):
            load_settings(spider_settings=wrong)
