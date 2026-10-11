import pytest

from pernix.settings import CoreSettings, Section


class RetrySettings(Section):
    __prefix__ = "retry"
    times: int = 3


def test_keys_are_prefixed_unless_the_prefix_is_empty() -> None:
    assert RetrySettings.key("times") == "retry.times"
    assert CoreSettings.key("concurrency") == "concurrency"


def test_sections_are_frozen_and_keyword_only() -> None:
    section = RetrySettings(times=5)

    with pytest.raises(AttributeError):
        section.times = 6  # type: ignore[misc]
    with pytest.raises(TypeError):
        RetrySettings(5)  # type: ignore[misc]
