from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pernix.headers import Headers

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["CapturedResponse", "RenderResult"]


@dataclass(frozen=True, slots=True)
class CapturedResponse:
    """A response the browser received, as it came over the network.

    Attributes:
        url: Its URL.
        status: Its status code.
        headers: Its headers, as the browser reports them: every value kept, but
            not in wire order.
        body: Its body, decoded from any `Content-Encoding`.
    """

    url: str
    status: int
    headers: Headers = field(default_factory=Headers)
    body: bytes = b""


@dataclass(frozen=True, slots=True, init=False)
class RenderResult:
    """What rendering a page gave besides its body: `response.browser` of a rendered Response.

    Attributes:
        document: The main document as it came over the network, before its scripts
            ran; `None` when not known, as in a Response built for a test.
        captured: The XHR and fetch responses `Render(capture=...)` matched, in the
            order they arrived.
        screenshots: PNG screenshots in the order they were taken, that of
            `Render(screenshot=True)` last.
        evaluated: The results of the `Evaluate` actions, in order.
    """

    document: CapturedResponse | None
    captured: tuple[CapturedResponse, ...]
    screenshots: tuple[bytes, ...]
    evaluated: tuple[Any, ...]

    def __init__(
        self,
        *,
        document: CapturedResponse | None = None,
        captured: Sequence[CapturedResponse] = (),
        screenshots: Sequence[bytes] = (),
        evaluated: Sequence[Any] = (),
    ) -> None:
        """Collect what rendering gave; tests build these for the Responses they answer with.

        Args:
            document: The main document as received.
            captured: The responses captured.
            screenshots: The screenshots taken.
            evaluated: The results of the `Evaluate` actions.
        """
        set_field = object.__setattr__
        set_field(self, "document", document)
        set_field(self, "captured", tuple(captured))
        set_field(self, "screenshots", tuple(screenshots))
        set_field(self, "evaluated", tuple(evaluated))
