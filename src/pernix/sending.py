"""How a Request is sent: as it is, under a Session leased for it, or through a proxy of the Run's."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from pernix.blocked_responses import rendered_instead
from pernix.errors import Blocked, LoginExpired, SessionError

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable
    from contextlib import AbstractAsyncContextManager

    from pernix.proxies.run_proxies import RunProxies
    from pernix.request import Request
    from pernix.response import Response
    from pernix.session import Session

__all__ = ["IndependentSending", "NotYet", "Sender", "Sending"]


class NotYet(Exception):
    """A Request cannot be sent yet, as no Session or no proxy is free for it; it is to wait elsewhere.

    Attributes:
        delay: Seconds until one may be, at the soonest; 0 if one is only busy.
        reason: What the Request waits for.
    """

    def __init__(self, delay: float, reason: str) -> None:
        """Create the signal.

        Args:
            delay: Seconds to wait.
            reason: What the Request waits for, such as `no Session available`.
        """
        super().__init__(f"{reason}; one may be in {delay:.2f} s")
        self.delay = delay
        self.reason = reason


@dataclass(slots=True)
class Sending:
    """A Request on its way, under the Session leased for it, if any.

    Attributes:
        request: The Request, naming the Session it is sent under, and the proxy it
            goes through when the Run's proxies gave it one.
        session: The Session; `None` for a Request sent without one.
        response: The Response, once it arrived, which tells how the Session and
            the proxy fared.
        error: The exception the fetch failed with, for a caller that handles it
            inside the block, which tells how they fared too.
        is_logged_out: Whether a Response shows the Session logged out; `None`
            when no Response is judged, as without a login, or during one.
        is_blocked: Whether a Response shows the site refusing the Session, or the
            proxy; `None` when no Response is judged, as during a login, or when a
            retry could change nothing.
        nested: Whether the fetch shares the Lease of a fetch it is nested in, such
            as one a download Middleware makes. Its Session logs in again only once
            that fetch ends, so a `LoginExpired` is for that fetch to retry, as is a
            `Blocked`, with another Session.
    """

    request: Request
    session: Session | None = None
    response: Response | None = None
    error: BaseException | None = None
    is_logged_out: Callable[[Response], bool] | None = field(default=None, repr=False)
    is_blocked: Callable[[Response], bool] | None = field(default=None, repr=False)
    nested: bool = False

    def received(self, response: Response) -> None:
        """Record the Response the fetch got.

        Args:
            response: The Response.

        Raises:
            pernix.errors.LoginExpired: The Response shows the Session logged out;
                it is recorded as the `error` too.
            pernix.errors.Blocked: The Response shows the site refusing its sender;
                it is recorded as the `error` too.
        """
        self.response = response
        if self.is_logged_out is not None and self.is_logged_out(response):
            self.error = LoginExpired(response)
            raise self.error
        if self.is_blocked is not None and self.is_blocked(response):
            self.error = Blocked(response)
            raise self.error


class Sender(Protocol):
    """Sends each Request, as the Run does: under a Session leased for it, or as it is."""

    def sending(self, request: Request, *, wait: float | None) -> AbstractAsyncContextManager[Sending]:
        """Send a Request inside the block, under what it is sent as.

        Call `received()` on what this gives once the Response arrived, or set its
        `error` for a failure handled inside the block.

        Args:
            request: The Request.
            wait: Seconds to wait for a Session; `None` to wait as long as it takes.

        Returns:
            An async context manager giving the Request as it is to be sent.

        Raises:
            NotYet: No Session, or no proxy, is free for the Request yet.
            pernix.errors.SessionError: The Request cannot be sent under the Session
                it names.
        """
        ...


class IndependentSending:
    """Sends Requests without a Session, each attempt through the Run's next proxy, if it has proxies.

    A Request with a proxy of its own, an empty one included, goes through it, or
    without one, instead. How each attempt went through its proxy is reported to the
    Run's proxies. A Request naming a Session is refused, as no Session Pool is
    enabled. A Response is judged Blocked only when a retry could change something:
    go through another of the Run's proxies, or be rendered, with `render_blocked`;
    otherwise it is left to its Callback.
    """

    def __init__(
        self,
        proxies: RunProxies | None = None,
        *,
        is_blocked: Callable[[Response], bool] | None = None,
        render_blocked: bool = False,
    ) -> None:
        """Create the sender.

        Args:
            proxies: The Run's proxies; `None` without any.
            is_blocked: Whether a Response shows the site refusing its sender;
                `None` to judge none.
            render_blocked: Whether a Request found Blocked is retried rendered.
        """
        self._proxies = proxies
        self._is_blocked = is_blocked
        self._render_blocked = render_blocked

    @asynccontextmanager
    async def sending(self, request: Request, *, wait: float | None) -> AsyncGenerator[Sending]:
        """Send a Request inside the block, through the Run's next proxy unless it has its own.

        Args:
            request: The Request.
            wait: Unused, as no Session is waited for.

        Yields:
            The Request as it is to be sent, naming its proxy.

        Raises:
            NotYet: No proxy is available yet.
            pernix.errors.SessionError: The Request names a Session.
        """
        if request.session is not None:
            raise SessionError(
                f"{request!r} names the Session {request.session!r}, but no Session Pool is enabled; "
                "enable one with sessions.enabled"
            )
        if self._proxies is None or request.proxy is not None:
            renders = self._render_blocked and rendered_instead(request) is not None
            yield Sending(request, is_blocked=self._is_blocked if renders else None)
            return
        proxy = await self._proxies.take()
        sending = Sending(request.replace(proxy=proxy), is_blocked=self._is_blocked)
        async with self._proxies.reporting(proxy, sending):
            yield sending
