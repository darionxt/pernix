from __future__ import annotations

import asyncio

import pytest

from pernix.fetchers.page_slots import PageSlot, PageSlots
from pernix.stats import Stats


async def _waiting(slots: PageSlots) -> asyncio.Task[PageSlot]:
    """Start taking a slot, and let the task wait for one."""
    task = asyncio.create_task(slots.take())
    await asyncio.sleep(0)
    return task


async def test_at_most_the_limit_is_taken_at_once() -> None:
    slots = PageSlots(2)
    first, second = await slots.take(), await slots.take()

    third = await _waiting(slots)

    assert not third.done()
    assert slots.taken == 2
    first.release()
    assert (await third).released is False
    assert slots.taken == 2
    second.release()
    third.result().release()
    assert slots.taken == 0


async def test_slots_go_to_the_fetches_in_the_order_they_asked() -> None:
    slots = PageSlots(1)
    held = await slots.take()
    waiting = [await _waiting(slots) for _ in range(3)]
    order: list[int] = []
    for n, task in enumerate(waiting):
        task.add_done_callback(lambda _task, n=n: order.append(n))

    for task in waiting:
        held.release()
        held = await task

    assert order == [0, 1, 2]


async def test_releasing_a_slot_again_frees_no_other() -> None:
    slots = PageSlots(1)
    slot = await slots.take()

    slot.release()
    slot.release()  # as when the holder it was handed to releases it too

    assert slot.released
    assert slots.taken == 0
    again = await slots.take()
    assert not (await _waiting(slots)).done()  # still one at most
    again.release()


async def test_a_wait_cancelled_takes_no_slot() -> None:
    slots = PageSlots(1)
    held = await slots.take()
    cancelled = await _waiting(slots)

    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    held.release()

    assert slots.taken == 0
    (await slots.take()).release()


async def test_the_slots_taken_and_the_waits_are_counted() -> None:
    stats = Stats()
    now = [0.0]
    slots = PageSlots(1, stats=stats, clock=lambda: now[0])
    held = await slots.take()
    waiting = await _waiting(slots)
    assert stats.get("browser.pages_open") == 1

    now[0] = 1.5
    held.release()
    (await waiting).release()

    assert stats.get("browser.page_waits") == 1
    assert stats.get("browser.page_wait_seconds") == 1.5
    assert stats.get("browser.pages_open") == 0


def test_a_limit_below_one_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        PageSlots(0)
