"""The client hints origins asked one HTTP fetch for, from its Session and from the responses of its redirect chain."""

from __future__ import annotations

from typing import TYPE_CHECKING

from pernix.fetchers.client_hints import keep_accepted, parse_hints
from pernix.urls import is_potentially_trustworthy, url_origin

if TYPE_CHECKING:
    from pernix.session import Session

__all__ = ["ChainHints"]


class ChainHints:
    """The client hints each origin asked one fetch for: those its Session keeps, and those its responses ask for.

    The responses of a top-level navigation or form submission, its redirects
    included, ask for hints with `Accept-CH`, in place of those their origin asked
    for before, in the Session too. A fetch without a Session starts with none, and
    keeps those its responses ask for within its own redirect chain only (ADR 0011).
    """

    def __init__(self, session: Session | None) -> None:
        """Gather the hints a fetch starts with.

        Args:
            session: The Session it is sent under, if any.
        """
        self._session = session
        self._asked: dict[str, tuple[str, ...]] = {} if session is None else dict(session.client_hints)

    def asked(self, url: str) -> tuple[str, ...]:
        """Return the hints the origin of a URL asked for.

        Args:
            url: The URL.

        Returns:
            The hints, in the order Chrome keeps them; none for an origin that
            asked for none.
        """
        origin = url_origin(url)
        return () if origin is None else self._asked.get(origin, ())

    def receive(self, url: str, accept_ch: str | None, critical_ch: str | None) -> tuple[str, ...]:
        """Keep the hints the response to a top-level navigation asks for, in the Session too.

        Args:
            url: The URL of the response.
            accept_ch: The value of its `Accept-CH` header, if any, its fields joined
                with commas.
            critical_ch: The value of its `Critical-CH` header, if any.

        Returns:
            The hints its `Critical-CH` names that its `Accept-CH` asks for and its
            origin had not asked for before, which Chrome sends the navigation again
            for; none without both headers.
        """
        if not is_potentially_trustworthy(url):
            return ()
        accepted = parse_hints(accept_ch)
        critical = parse_hints(critical_ch)
        before = self.asked(url)
        missing = (
            ()
            if accepted is None or critical is None
            else tuple(hint for hint in critical if hint in accepted and hint not in before)
        )
        keep_accepted(self._asked, url, accept_ch)
        if self._session is not None:  # whatever the chain held, as another fetch of the Session may have changed it
            keep_accepted(self._session.client_hints, url, accept_ch)
        return missing
