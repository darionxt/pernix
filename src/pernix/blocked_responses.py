"""Blocked Responses: telling those in which a site refuses their sender, and what a retry of one may change."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from pernix.request_kinds import request_kind

if TYPE_CHECKING:
    from collections.abc import Callable

    from pernix.request import Request
    from pernix.response import Response
    from pernix.stats import Stats

__all__ = ["BlockedResponses", "rendered_instead"]

logger = logging.getLogger(__name__)


class BlockedResponses:
    """Tells the Responses in which a site refuses their sender, as the Spider's `is_blocked()` judges, counting them.

    Each Response found Blocked is counted as `responses.blocked`.
    """

    def __init__(self, is_blocked: Callable[[Response], bool], *, stats: Stats) -> None:
        """Wire the judgment to the Spider's hook.

        Args:
            is_blocked: Whether a Response shows the site refusing the Session it was
                fetched under, or its proxy.
            stats: Where the Responses found Blocked are counted.
        """
        self._is_blocked = is_blocked
        self._stats = stats

    def blocked(self, response: Response) -> bool:
        """Whether a Response shows the site refusing its sender.

        A judgment that raises is logged, and the Response taken not to be Blocked.

        Args:
            response: The Response.

        Returns:
            Whether it is to be retried with another Session, through another proxy,
            or rendered.
        """
        try:
            blocked = bool(self._is_blocked(response))
        except Exception:
            logger.exception("Judging whether %r is blocked failed; taking it not to be", response)
            return False
        if blocked:
            self._stats.inc("responses.blocked")
        return blocked


def rendered_instead(request: Request) -> Request | None:
    """The Request to retry, rendered, in place of an HTTP one found Blocked.

    Args:
        request: The Request found Blocked.

    Returns:
        The Request with `render=True`; `None` if it renders already, names its
        Fetcher, or is no top-level navigation or form submission, which the browser
        alone loads as a page (`pernix.request_kinds`).
    """
    if request.render or request.fetcher is not None:
        return None
    try:
        kind = request_kind(request)
    except ValueError:
        return None
    return request.replace(render=True) if kind in ("navigation", "form") else None
