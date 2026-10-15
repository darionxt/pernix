"""The examples of the documentation, run as they are written."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pytest

README = Path(__file__).parent.parent / "README.md"


def _example(section: str) -> str:
    """The first Python block of a README section."""
    text = README.read_text()
    start = text.index("```python\n", text.index(f"## {section}\n")) + len("```python\n")
    return text[start : text.index("```\n", start)]


def test_the_readme_testing_example_passes(pytester: pytest.Pytester) -> None:
    pytester.makeini("[pytest]\nasyncio_mode = auto\nasyncio_default_fixture_loop_scope = function\n")
    pytester.makepyfile(test_example=_example("Testing"))

    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)
