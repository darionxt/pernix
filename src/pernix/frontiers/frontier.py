"""The Frontier contract: Requests waiting to be fetched, handed out under Leases (ADR 0003).

A Request added to the Frontier is deduplicated and stored in one atomic step. A
Worker claims it under a Lease, renews the Lease while it works, and settles it:
`ack` when done, `nack` to try again later, or `dead` to keep it as a Dead Request.
Every write names the Lease, and only the latest Lease of a Request can write, so a
Worker whose Lease expired cannot overwrite what happened since. Delivery is
at-least-once: a Request whose Lease expires is handed out again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from collections.abc import Collection, Sequence

    from pernix.request import Request

__all__ = ["Frontier", "FrontierCounts", "Lease", "host_slot"]


@dataclass(frozen=True, slots=True)
class Lease:
    """A time-limited claim on one Request in the Frontier.

    Attributes:
        id: The Frontier's id of the Request.
        token: Increases with every claim; writes with an older token have no effect.
        attempt: Which attempt this is, from 1. Attempts settled with
            `nack(count_attempt=False)` do not count; expired Leases do.
        expires_at: When the Lease ends unless renewed, on the Frontier's clock.
        slot: The concurrency slot of the Request, such as its host.
        request: The Request.
        rendered: Whether the Request goes to the browser Fetcher, as
            `pernix.fetchers.is_rendered()` tells.
    """

    id: str
    token: int
    attempt: int
    expires_at: float
    slot: str
    request: Request
    rendered: bool = False


@dataclass(frozen=True, slots=True)
class FrontierCounts:
    """How many Requests are in each state.

    Attributes:
        ready: Waiting to be claimed.
        delayed: Returned with a delay that has not passed yet.
        leased: Claimed under a Lease that has not expired.
        done: Acknowledged.
        dead: Kept as Dead Requests.
    """

    ready: int
    delayed: int
    leased: int
    done: int
    dead: int

    @property
    def unfinished(self) -> int:
        """Requests that are ready, delayed, or leased; the Run is idle when there are none."""
        return self.ready + self.delayed + self.leased


class Frontier(Protocol):
    """Stores the Requests of a Run, deduplicates them, and hands them out under Leases.

    Times are seconds on the Frontier's own clock, which also decides when Leases
    expire and delays pass. Implementations must pass the conformance suite in
    `pernix.testing.frontier`.
    """

    async def add(self, requests: Sequence[Request]) -> list[bool]:
        """Deduplicate and store Requests, atomically.

        Requests with `dont_filter` are always stored. Others are stored unless a
        Request with the same Fingerprint (their own `fingerprint`, or the
        Frontier's Fingerprint function's) was added before, in this call or earlier.

        Args:
            requests: The Requests, in order.

        Returns:
            For each Request, whether it was stored.
        """
        ...

    async def claim(
        self,
        worker_id: str,
        limit: int,
        ttl: float,
        skip_slots: Collection[str] = frozenset(),
        *,
        rendered: bool | None = None,
    ) -> list[Lease]:
        """Lease ready Requests: higher priorities first, oldest first within a priority.

        Args:
            worker_id: Identifies the claiming Worker.
            limit: The most Requests to lease, at least 1.
            ttl: Seconds until the Leases expire unless renewed; positive.
            skip_slots: Concurrency slots whose Requests to leave for later.
            rendered: Lease only the Requests that go to the browser Fetcher, as
                `pernix.fetchers.is_rendered()` tells of them when they are added
                (`True`), only the others (`False`), or both (`None`).

        Returns:
            The Leases; empty if nothing is ready.
        """
        ...

    async def renew(self, lease: Lease, ttl: float) -> Lease | None:
        """Extend a Lease that has not expired.

        Args:
            lease: The Lease.
            ttl: Seconds from now until it expires; positive.

        Returns:
            The renewed Lease, or `None` if the Lease is no longer valid.
        """
        ...

    async def ack(self, lease: Lease) -> bool:
        """Mark a leased Request as done.

        Args:
            lease: The Lease.

        Returns:
            `False` if the Lease is no longer valid, and nothing changed.
        """
        ...

    async def nack(self, lease: Lease, *, delay: float = 0.0, count_attempt: bool = True, reason: str = "") -> bool:
        """Return a leased Request, to be claimed again after a delay.

        Args:
            lease: The Lease.
            delay: Seconds before it can be claimed again; it then queues behind the
                Requests of its priority that are ready.
            count_attempt: Count this as a failed attempt.
            reason: Why, kept for inspection.

        Returns:
            `False` if the Lease is no longer valid, and nothing changed.
        """
        ...

    async def dead(self, lease: Lease, reason: str) -> bool:
        """Keep a leased Request as a Dead Request; it is never handed out again.

        Args:
            lease: The Lease.
            reason: Why it failed for good, kept for inspection.

        Returns:
            `False` if the Lease is no longer valid, and nothing changed.
        """
        ...

    async def counts(self) -> FrontierCounts:
        """Count the Requests in each state."""
        ...

    async def wait(self) -> None:
        """Wait until something may have changed.

        Changes include added and settled Requests, delays passing, and Leases
        expiring. Returning without a change is allowed, for example after a polling
        interval, so callers check again; bound the wait with `asyncio.timeout()`.
        """
        ...

    async def close(self) -> None:
        """Release resources; stored Requests are never deleted."""
        ...


def host_slot(request: Request) -> str:
    """Return the default concurrency slot of a Request: its lower-cased host name.

    Args:
        request: The Request.

    Returns:
        The host name, or an empty string for URLs without one.
    """
    return urlsplit(request.url).hostname or ""
