from __future__ import annotations

import difflib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["did_you_mean"]


def did_you_mean(word: str, candidates: Iterable[str]) -> str:
    """Suggest the candidate closest to a misspelled word.

    Args:
        word: The word that was not recognized.
        candidates: The valid words.

    Returns:
        `"; did you mean '<candidate>'?"`, or an empty string when nothing is close.
    """
    matches = difflib.get_close_matches(word, list(candidates), n=1, cutoff=0.6)
    return f"; did you mean {matches[0]!r}?" if matches else ""
