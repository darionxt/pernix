from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from typing import ClassVar

import pytest

from pernix import events
from pernix.events import Dispatch, Event, EventBus, Veto


@dataclass(frozen=True)
class Ping(Event):
    value: int = 0


@dataclass(frozen=True)
class SubPing(Ping):
    pass


@dataclass(frozen=True)
class Shutdown(Event):
    reverse: ClassVar[bool] = True


@dataclass(frozen=True)
class Admit(Event):
    dispatch: ClassVar[Dispatch] = Dispatch.GATE
    url: str = ""


@dataclass(frozen=True)
class StrictAdmit(Admit):
    fail_closed: ClassVar[bool] = True


@dataclass(frozen=True)
class Rewrite(Event):
    dispatch: ClassVar[Dispatch] = Dispatch.TRANSFORM
    text: str = ""


def recorder(calls: list[str], name: str):
    def subscriber(event: Event) -> None:
        calls.append(name)

    return subscriber


# Observe


async def test_observe_runs_sync_and_async_subscribers_in_priority_then_registration_order() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def async_subscriber(event: Ping) -> None:
        await asyncio.sleep(0)
        calls.append("async")

    bus.on(Ping, recorder(calls, "second"), priority=10)
    bus.on(Ping, async_subscriber, priority=5)
    bus.on(Ping, recorder(calls, "third"), priority=10)
    bus.on(Ping, recorder(calls, "first"), priority=-1)

    await bus.emit(Ping())

    assert calls == ["first", "async", "second", "third"]


async def test_shutdown_events_run_subscribers_in_reverse() -> None:
    bus = EventBus()
    calls: list[str] = []
    for name in ("a", "b", "c"):
        bus.on(Shutdown, recorder(calls, name))

    await bus.emit(Shutdown())

    assert calls == ["c", "b", "a"]


async def test_subscribers_receive_subclass_events() -> None:
    bus = EventBus()
    received: list[Event] = []
    bus.on(Event, received.append)
    bus.on(Ping, received.append)

    await bus.emit(SubPing(1))

    assert received == [SubPing(1), SubPing(1)]


async def test_a_failing_subscriber_is_logged_and_isolated(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    calls: list[str] = []

    def broken(event: Ping) -> None:
        raise RuntimeError("boom")

    bus.on(Ping, broken)
    bus.on(Ping, recorder(calls, "after"))

    await bus.emit(Ping())

    assert calls == ["after"]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "broken failed handling Ping" in record.getMessage()


async def test_slow_async_subscribers_time_out_and_others_still_run(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus(timeout=5)
    calls: list[str] = []

    async def slow(event: Ping) -> None:
        await asyncio.sleep(10)

    bus.on(Ping, slow, timeout=0.01)
    bus.on(Ping, recorder(calls, "after"))

    async with asyncio.timeout(2):
        await bus.emit(Ping())

    assert calls == ["after"]
    assert any("slow timed out handling Ping" in r.getMessage() for r in caplog.records)


async def test_the_bus_timeout_applies_by_default() -> None:
    bus = EventBus(timeout=0.01)
    finished: list[bool] = []

    async def slow(event: Ping) -> None:
        await asyncio.sleep(10)
        finished.append(True)

    bus.on(Ping, slow)
    async with asyncio.timeout(2):
        await bus.emit(Ping())

    assert finished == []


async def test_awaitables_returned_by_sync_subscribers_are_awaited() -> None:
    bus = EventBus()
    calls: list[str] = []

    async def work() -> None:
        calls.append("awaited")

    bus.on(Ping, lambda event: work())

    await bus.emit(Ping())

    assert calls == ["awaited"]


async def test_cancellation_is_never_swallowed() -> None:
    bus = EventBus()
    started = asyncio.Event()

    async def waits(event: Ping) -> None:
        started.set()
        await asyncio.sleep(10)

    bus.on(Ping, waits)
    task = asyncio.create_task(bus.emit(Ping()))
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_dispatch_uses_a_snapshot_of_subscribers() -> None:
    bus = EventBus()
    calls: list[str] = []

    def adds_another(event: Ping) -> None:
        calls.append("first")
        bus.on(Ping, recorder(calls, "late"))

    bus.on(Ping, adds_another)

    await bus.emit(Ping())
    assert calls == ["first"]

    await bus.emit(Ping())
    assert calls == ["first", "first", "late"]


async def test_unsubscribe_removes_the_subscription() -> None:
    bus = EventBus()
    calls: list[str] = []
    unsubscribe = bus.on(Ping, recorder(calls, "a"))

    unsubscribe()
    unsubscribe()
    await bus.emit(Ping())

    assert calls == []


def test_only_event_classes_can_be_subscribed() -> None:
    with pytest.raises(TypeError, match="can only subscribe to Event classes"):
        EventBus().on(str, print)  # type: ignore[arg-type]


async def test_events_must_be_dispatched_by_their_mode() -> None:
    bus = EventBus()

    with pytest.raises(TypeError, match=r"Admit has dispatch 'gate'; dispatch it with EventBus\.gate\(\)"):
        await bus.emit(Admit())
    with pytest.raises(TypeError, match=r"Ping has dispatch 'observe'; dispatch it with EventBus\.emit\(\)"):
        await bus.gate(Ping())
    with pytest.raises(TypeError, match=r"dispatch it with EventBus\.transform\(\)"):
        await bus.emit(Rewrite())


# Gate


async def test_gate_returns_the_first_veto_and_skips_the_rest() -> None:
    bus = EventBus()
    calls: list[str] = []

    def allow(event: Admit) -> None:
        calls.append("allow")

    def deny(event: Admit) -> Veto | None:
        calls.append("deny")
        return Veto("offsite") if "other.example" in event.url else None

    bus.on(Admit, allow)
    bus.on(Admit, deny)
    bus.on(Admit, recorder(calls, "never"))

    assert await bus.gate(Admit("https://example.com/")) is None
    assert await bus.gate(Admit("https://other.example/")) == Veto("offsite")
    assert calls == ["allow", "deny", "never", "allow", "deny"]


async def test_failing_gate_subscribers_are_ignored_when_fail_open(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()

    def broken(event: Admit) -> None:
        raise RuntimeError("boom")

    bus.on(Admit, broken)

    assert await bus.gate(Admit()) is None
    assert any("broken failed handling Admit" in r.getMessage() for r in caplog.records)


async def test_failing_or_slow_gate_subscribers_veto_when_fail_closed() -> None:
    bus = EventBus()

    def broken(event: Admit) -> None:
        raise RuntimeError("boom")

    async def slow(event: Admit) -> None:
        await asyncio.sleep(10)

    unsubscribe = bus.on(StrictAdmit, broken)
    veto = await bus.gate(StrictAdmit())
    assert veto is not None
    assert "broken failed: RuntimeError('boom')" in veto.reason

    unsubscribe()
    bus.on(StrictAdmit, slow, timeout=0.01)
    veto = await bus.gate(StrictAdmit())
    assert veto is not None
    assert "slow failed" in veto.reason


# Transform


async def test_transform_pipes_the_event_through_each_subscriber() -> None:
    bus = EventBus()
    bus.on(Rewrite, lambda event: replace(event, text=event.text + "a"))
    bus.on(Rewrite, lambda event: None)

    async def async_step(event: Rewrite) -> Rewrite:
        return replace(event, text=event.text + "b")

    bus.on(Rewrite, async_step)

    assert await bus.transform(Rewrite("x")) == Rewrite("xab")


async def test_transform_discards_bad_results_and_failures(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()

    def broken(event: Rewrite) -> Rewrite:
        raise RuntimeError("boom")

    bus.on(Rewrite, lambda event: "not an event")
    bus.on(Rewrite, broken)
    bus.on(Rewrite, lambda event: replace(event, text="kept"))

    assert await bus.transform(Rewrite("x")) == Rewrite("kept")
    messages = [r.getMessage() for r in caplog.records]
    assert any("Ignoring 'not an event'" in message for message in messages)
    assert any("broken failed handling Rewrite" in message for message in messages)


# Marked methods


class Listener:
    def __init__(self) -> None:
        self.calls: list[str] = []

    @events.on(Ping, priority=1)
    def ping(self, event: Ping) -> None:
        self.calls.append(f"ping {event.value}")

    @events.on(Shutdown)
    @events.on(Ping, priority=0)
    async def both(self, event: Event) -> None:
        self.calls.append(f"both {type(event).__name__}")

    def unmarked(self, event: Event) -> None:
        self.calls.append("unmarked")


async def test_subscribe_methods_registers_marked_methods() -> None:
    bus = EventBus()
    listener = Listener()

    assert bus.subscribe_methods(listener) == 3
    await bus.emit(Ping(7))
    await bus.emit(Shutdown())

    assert listener.calls == ["both Ping", "ping 7", "both Shutdown"]


async def _cancelled_by_itself(event: Event) -> None:
    future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
    future.cancel()
    await future


async def test_a_subscriber_raising_cancelled_error_by_itself_is_isolated(caplog: pytest.LogCaptureFixture) -> None:
    bus = EventBus()
    calls: list[str] = []
    bus.on(Ping, _cancelled_by_itself)
    bus.on(Ping, recorder(calls, "after"))

    await bus.emit(Ping())

    assert calls == ["after"]
    assert any("_cancelled_by_itself failed handling Ping" in r.getMessage() for r in caplog.records)


async def test_gates_treat_a_self_cancelled_subscriber_as_a_failure() -> None:
    bus = EventBus()
    bus.on(Admit, _cancelled_by_itself)
    bus.on(StrictAdmit, _cancelled_by_itself)

    assert await bus.gate(Admit()) is None
    veto = await bus.gate(StrictAdmit())
    assert veto is not None
    assert "raised CancelledError" in veto.reason


async def test_transforms_discard_a_self_cancelled_subscriber() -> None:
    bus = EventBus()
    bus.on(Rewrite, _cancelled_by_itself)
    bus.on(Rewrite, lambda event: replace(event, text="kept"))

    assert await bus.transform(Rewrite("x")) == Rewrite("kept")
