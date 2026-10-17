"""Logging Sessions in with a Spider's login, and telling the Responses that show one logged out."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from pernix.account import Account
    from pernix.response import Response
    from pernix.session import Session

__all__ = ["Login", "SessionLogin"]

logger = logging.getLogger(__name__)

type Login = Callable[[Session, Account | None], Awaitable[None]]
"""Logs a Session in, with its Account if it has one, such as `Spider.login()`."""


class SessionLogin:
    """Logs the Sessions of a Session Pool in, and tells which Responses show one logged out.

    Attributes:
        accounts: The Accounts Sessions are bound to, by id.
    """

    def __init__(
        self, login: Login, is_logged_out: Callable[[Response], bool], *, accounts: Iterable[Account] = ()
    ) -> None:
        """Wire the login to the Spider's hooks.

        Args:
            login: Logs a Session in; it fails by raising.
            is_logged_out: Whether a Response fetched under a Session shows it
                logged out.
            accounts: The Accounts Sessions are bound to.
        """
        self._login = login
        self._is_logged_out = is_logged_out
        self.accounts = {account.id: account for account in accounts}

    async def log_in(self, session: Session) -> None:
        """Log a Session in, with the Account it is bound to, if any.

        Args:
            session: The Session.

        Raises:
            Exception: Whatever the login raised; the login failed.
        """
        account = None if session.account is None else self.accounts.get(session.account)
        await self._login(session, account)

    def logged_out(self, response: Response) -> bool:
        """Whether a Response shows the Session it was fetched under logged out.

        A judgment that raises is logged, and the Session taken to be logged in.

        Args:
            response: The Response.

        Returns:
            Whether the Session is to log in again.
        """
        try:
            return bool(self._is_logged_out(response))
        except Exception:
            logger.exception(
                "Judging whether the Session of %r is logged out failed; taking it to be logged in", response
            )
            return False
