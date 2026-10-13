from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["RunResult"]


@dataclass(frozen=True, slots=True)
class RunResult:
    """How a Run that was not interrupted by an error or a cancellation ended.

    Attributes:
        run_id: Identifies the Run.
        reason: `"finished"` when it ended on its own, `"drained"` after a Drain, and
            `"forced"` after a Force Stop.
        stats: The final stats snapshot.
    """

    run_id: str
    reason: str
    stats: Mapping[str, Any]
