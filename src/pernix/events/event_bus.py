from __future__ import annotations

import asyncio
import dataclasses
import inspect
import itertools
import logging
from collections.abc import Callable
from typing import Any, cast

from pernix.events.event import Dispatch, Event, Veto

__all__ = ["EventBus", "Subscriber", "on"]

logger = logging.getLogger(__name__)

type Subscriber = Callable[[Any], object]
"""Receives an Event; may be sync or async, and returns `None`, a `Veto`, or a replacement Event."""

_SUBSCRIPTIONS_ATTRIBUTE = "__pernix_subscriptions__"


@dataclasses.dataclass(frozen=True, slots=True)
class _Subscription:
    event_type: type[Event]
    subscriber: Subscriber
    priority: int
    timeout: float | None
    sequence: int


@dataclasses.dataclass(frozen=True, slots=True)
class _Marker:
    event_type: type[Event]
    priority: int
    timeout: float | None


def on[F: Callable[..., object]](
    event_type: type[Event], *, priority: int = 0, timeout: float | None = None
) -> Callable[[F], F]:
    """Mark a method as a subscriber, to be subscribed when its object is registered.

    Components such as Extensions use it on their methods; the Crawler subscribes the
    marked methods of every component it builds.

    Args:
        event_type: The Event class to receive, including its subclasses.
        priority: Lower priorities run first; ties keep registration order.
        timeout: Seconds before an async subscriber is cancelled; defaults to the
            bus timeout.

    Returns:
        A decorator that returns the method unchanged.

    Example:
        >>> class Notify(Extension):
        ...     @events.on(events.SpiderClosed)
        ...     async def closed(self, event): ...
    """

    def mark(method: F) -> F:
        markers: list[_Marker] = [
            *getattr(method, _SUBSCRIPTIONS_ATTRIBUTE, ()),
            _Marker(event_type, priority, timeout),
        ]
        setattr(method, _SUBSCRIPTIONS_ATTRIBUTE, tuple(markers))
        return method

    return mark


class EventBus:
    """Delivers Events to subscribers, in `(priority, registration order)`.

    Every Event declares how it is dispatched (see `Dispatch`); call the method that
    matches: `emit()` for observe, `gate()` for gate, and `transform()` for transform
    Events. Subscribers may be sync or async; a returned awaitable is always awaited.
    Each async subscriber runs under a timeout. Cancelling the dispatching task always
    propagates; a `CancelledError` that a subscriber raises on its own, without the
    task being cancelled, is handled like any other subscriber failure.
    """

    def __init__(self, *, timeout: float = 30.0) -> None:
        """Create a bus with no subscribers.

        Args:
            timeout: Default seconds before an async subscriber is cancelled.
        """
        self._timeout = timeout
        self._subscriptions: list[_Subscription] = []
        self._sequence = itertools.count()

    def on(
        self,
        event_type: type[Event],
        subscriber: Subscriber,
        *,
        priority: int = 0,
        timeout: float | None = None,
    ) -> Callable[[], None]:
        """Subscribe to an Event class and its subclasses.

        Args:
            event_type: The Event class to receive.
            subscriber: Called with each Event; sync or async.
            priority: Lower priorities run first; ties keep registration order.
            timeout: Seconds before an async subscriber is cancelled; defaults to the
                bus timeout.

        Returns:
            A function that removes this subscription.

        Raises:
            TypeError: `event_type` is not an Event class.
        """
        if not _is_event_class(event_type):  # also guards untyped callers
            raise TypeError(f"can only subscribe to Event classes, not {event_type!r}")
        subscription = _Subscription(event_type, subscriber, priority, timeout, next(self._sequence))
        self._subscriptions.append(subscription)

        def unsubscribe() -> None:
            if subscription in self._subscriptions:
                self._subscriptions.remove(subscription)

        return unsubscribe

    def subscribe_methods(self, component: object) -> int:
        """Subscribe every method of an object that is marked with `on()`.

        Args:
            component: An Extension or any other component.

        Returns:
            How many subscriptions were added.
        """
        added = 0
        for name in dir(type(component)):
            markers: tuple[_Marker, ...] = getattr(getattr(type(component), name, None), _SUBSCRIPTIONS_ATTRIBUTE, ())
            for marker in markers:
                method = cast("Subscriber", getattr(component, name))
                self.on(marker.event_type, method, priority=marker.priority, timeout=marker.timeout)
                added += 1
        return added

    async def emit(self, event: Event) -> None:
        """Deliver an observe Event to every subscriber.

        A subscriber that raises or times out is logged; the others still run.

        Args:
            event: The Event; its `dispatch` must be `Dispatch.OBSERVE`.
        """
        _require(event, Dispatch.OBSERVE)
        for subscription in self._matching(event):
            await self._call(subscription, event)

    async def gate(self, event: Event) -> Veto | None:
        """Ask every subscriber whether a gate Event may proceed.

        Subscribers run in order until one returns a `Veto`. A subscriber that raises
        or times out vetoes the Event if it is `fail_closed`, and is ignored otherwise.

        Args:
            event: The Event; its `dispatch` must be `Dispatch.GATE`.

        Returns:
            The first `Veto`, or `None` if every subscriber let the Event through.
        """
        _require(event, Dispatch.GATE)
        for subscription in self._matching(event):
            try:
                result = await self._call(subscription, event, isolate=False)
            except Exception as exc:
                _log_failure(subscription, event, exc)
                if type(event).fail_closed:
                    return Veto(f"{_describe(subscription.subscriber)} failed: {exc!r}")
                continue
            if isinstance(result, Veto):
                return result
        return None

    async def transform[E: Event](self, event: E) -> E:
        """Pass a transform Event through every subscriber in turn.

        A subscriber returns a replacement Event of the same class, or `None` to keep
        it. A subscriber that raises, times out, or returns anything else is logged and
        its result discarded.

        Args:
            event: The Event; its `dispatch` must be `Dispatch.TRANSFORM`.

        Returns:
            The Event after every subscriber has seen it.
        """
        _require(event, Dispatch.TRANSFORM)
        for subscription in self._matching(event):
            result = await self._call(subscription, event)
            if isinstance(result, type(event)):
                event = result
            elif result is not None:
                logger.error(
                    "Ignoring %r returned by %s for %s: expected a %s or None",
                    result,
                    _describe(subscription.subscriber),
                    type(event).__name__,
                    type(event).__name__,
                )
        return event

    def _matching(self, event: Event) -> list[_Subscription]:
        # A snapshot: subscriptions added or removed during dispatch apply to the next Event.
        matching = [s for s in self._subscriptions if isinstance(event, s.event_type)]
        matching.sort(key=lambda s: (s.priority, s.sequence), reverse=type(event).reverse)
        return matching

    async def _call(self, subscription: _Subscription, event: Event, *, isolate: bool = True) -> object:
        try:
            try:
                result = subscription.subscriber(event)
                if inspect.isawaitable(result):
                    timeout = self._timeout if subscription.timeout is None else subscription.timeout
                    async with asyncio.timeout(timeout):
                        result = await result
            except asyncio.CancelledError as exc:
                if _dispatch_is_cancelled():
                    raise
                # The subscriber raised CancelledError on its own, for example by awaiting an
                # already-cancelled Future. Propagating it would silently end the calling task
                # as if the Run were cancelled, so it counts as an ordinary subscriber failure.
                raise RuntimeError(f"{_describe(subscription.subscriber)} raised CancelledError") from exc
        except Exception as exc:
            if not isolate:
                raise
            _log_failure(subscription, event, exc)
            return None
        return result


def _dispatch_is_cancelled() -> bool:
    """Whether the task dispatching the Event has a pending cancellation request."""
    task = asyncio.current_task()
    return task is None or task.cancelling() > 0


def _is_event_class(value: object) -> bool:
    return isinstance(value, type) and issubclass(value, Event)


def _require(event: Event, dispatch: Dispatch) -> None:
    if type(event).dispatch is not dispatch:
        raise TypeError(
            f"{type(event).__name__} has dispatch {type(event).dispatch.value!r}; "
            f"dispatch it with EventBus.{_METHODS[type(event).dispatch]}()"
        )


_METHODS = {Dispatch.OBSERVE: "emit", Dispatch.GATE: "gate", Dispatch.TRANSFORM: "transform"}


def _log_failure(subscription: _Subscription, event: Event, exc: Exception) -> None:
    if isinstance(exc, TimeoutError):
        logger.error("Subscriber %s timed out handling %s", _describe(subscription.subscriber), type(event).__name__)
    else:
        logger.error(
            "Subscriber %s failed handling %s",
            _describe(subscription.subscriber),
            type(event).__name__,
            exc_info=exc,
        )


def _describe(subscriber: Subscriber) -> str:
    return getattr(subscriber, "__qualname__", repr(subscriber))
