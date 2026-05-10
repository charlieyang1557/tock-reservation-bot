"""Tests for the prep/click split of the confirm lock (Phase B3.1).

Old: `async with self._confirm_lock` wrapped the entire _confirm_booking,
including payment detection, CVC fill, wait for confirm button — all of
which serialized N concurrent tasks unnecessarily. Each task with its
own page does idempotent prep work; only the actual click-and-verify
needs to be serial.

New design:
  1. Prep — `await self._prepare_for_confirm(page, slot)` — runs WITHOUT
     the lock. N tasks can prep concurrently.
  2. Click + verify — under `async with self._confirm_lock`. Re-checks
     `booking_won` and `_confirm_attempted` inside the lock; only one
     task's click + verification + soft-win bookkeeping runs.

Safety invariants preserved:
  - asyncio.Lock around the click and verification (never two clicks)
  - _confirm_attempted set INSIDE the lock immediately before the click
    so even if our verification fails the next task can't try again
  - Soft-win disk persistence (booking_uncertain.json) still atomic
    with the click
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


def _make_slot(time_str: str):
    return AvailableSlot(
        slot_date=date(2026, 5, 15),
        slot_time=time_str,
        day_of_week="Friday",
    )


# ---------------------------------------------------------------------------
# Method existence: the split has occurred
# ---------------------------------------------------------------------------

def test_prepare_for_confirm_method_exists():
    """The prep step must be a separate method that can be called
    without the lock held."""
    booker = _make_booker()
    assert hasattr(booker, "_prepare_for_confirm")
    assert callable(booker._prepare_for_confirm)


def test_execute_confirm_click_method_exists():
    """The click+verify step must be a separate method that runs
    under the lock."""
    booker = _make_booker()
    assert hasattr(booker, "_execute_confirm_click_and_verify")
    assert callable(booker._execute_confirm_click_and_verify)


# ---------------------------------------------------------------------------
# Concurrency: prep runs in parallel, click is serialized
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_two_tasks_prepare_concurrently_then_one_clicks():
    """Two tasks both reach prep concurrently (interleaved). Only one
    crosses into the locked click section."""
    booker = _make_booker()

    in_prep_count = 0
    in_prep_event = asyncio.Event()
    can_finish_prep = asyncio.Event()
    click_count = 0

    async def fake_prepare(page, slot, **_kwargs):
        nonlocal in_prep_count
        in_prep_count += 1
        if in_prep_count >= 2:
            # Both tasks have entered prep — let them finish
            in_prep_event.set()
        # Wait for both tasks to be in prep before either finishes
        await asyncio.wait_for(in_prep_event.wait(), timeout=2.0)
        return True

    async def fake_click(page, slot):
        nonlocal click_count
        click_count += 1
        return True  # first click wins

    # Stub out the page-creation and other pre-confirm steps
    page1 = AsyncMock()
    page1.is_closed = MagicMock(return_value=False)
    page1.url = "https://www.exploretock.com/test/checkout/abc"
    page2 = AsyncMock()
    page2.is_closed = MagicMock(return_value=False)
    page2.url = "https://www.exploretock.com/test/checkout/abc"
    booker.browser.new_page = AsyncMock(side_effect=[page1, page2])

    booking_won = asyncio.Event()
    slot1 = _make_slot("5:00 PM")
    slot2 = _make_slot("8:00 PM")

    with patch.object(
        booker, "_prepare_for_confirm", side_effect=fake_prepare
    ), patch.object(
        booker, "_execute_confirm_click_and_verify",
        side_effect=fake_click,
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ):
        results = await asyncio.gather(
            booker._book_single(slot1, booking_won),
            booker._book_single(slot2, booking_won),
            return_exceptions=True,
        )

    assert in_prep_count == 2, (
        f"Both tasks must have entered prep concurrently; got {in_prep_count}"
    )
    assert click_count == 1, (
        f"Exactly one task must have clicked; got {click_count}"
    )


@pytest.mark.asyncio
async def test_confirm_attempted_blocks_second_click_after_prep():
    """Two tasks both finish prep. The first acquires the lock and sets
    _confirm_attempted. The second acquires the lock next, sees the
    flag set, aborts WITHOUT clicking — even though prep succeeded."""
    booker = _make_booker()

    click_count = 0
    abort_count = 0

    # Prep always succeeds for both tasks
    async def fake_prepare(page, slot, **_kwargs):
        return True

    # Track who clicked vs aborted
    async def fake_click(page, slot):
        nonlocal click_count
        click_count += 1
        # Simulate a slow click+verify so the second task is forced to
        # wait on the lock
        await asyncio.sleep(0.05)
        return True

    page1 = AsyncMock()
    page1.is_closed = MagicMock(return_value=False)
    page1.url = "https://www.exploretock.com/test/checkout/abc"
    page2 = AsyncMock()
    page2.is_closed = MagicMock(return_value=False)
    page2.url = "https://www.exploretock.com/test/checkout/abc"
    booker.browser.new_page = AsyncMock(side_effect=[page1, page2])

    def count_abort(*args, **kwargs):
        nonlocal abort_count
        abort_count += 1
    booker.notifier.booking_aborted = MagicMock(side_effect=count_abort)

    booking_won = asyncio.Event()
    slot1 = _make_slot("5:00 PM")
    slot2 = _make_slot("8:00 PM")

    with patch.object(
        booker, "_prepare_for_confirm", side_effect=fake_prepare
    ), patch.object(
        booker, "_execute_confirm_click_and_verify",
        side_effect=fake_click,
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ):
        await asyncio.gather(
            booker._book_single(slot1, booking_won),
            booker._book_single(slot2, booking_won),
            return_exceptions=True,
        )

    assert click_count == 1, f"Exactly one click expected; got {click_count}"
    assert abort_count >= 1, "Loser task must have logged an abort"


@pytest.mark.asyncio
async def test_no_double_booking_under_high_concurrency():
    """Fuzz: 5 racing slots, all reach prep concurrently. Exactly one
    click ever happens, regardless of ordering."""
    booker = _make_booker()

    click_count = 0

    async def fake_prepare(page, slot, **_kwargs):
        # Simulate non-trivial prep that yields multiple times
        for _ in range(3):
            await asyncio.sleep(0)
        return True

    async def fake_click(page, slot):
        nonlocal click_count
        # Yield BEFORE incrementing so other tasks get a chance to be
        # scheduled — the lock is the only thing preventing a race
        await asyncio.sleep(0)
        click_count += 1
        return True

    pages = [AsyncMock() for _ in range(5)]
    for p in pages:
        p.is_closed = MagicMock(return_value=False)
        p.url = "https://www.exploretock.com/test/checkout/abc"
        p.close = AsyncMock()
    booker.browser.new_page = AsyncMock(side_effect=pages)

    booking_won = asyncio.Event()
    slots = [_make_slot(f"{h}:00 PM") for h in (5, 6, 7, 8, 9)]

    with patch.object(
        booker, "_prepare_for_confirm", side_effect=fake_prepare
    ), patch.object(
        booker, "_execute_confirm_click_and_verify",
        side_effect=fake_click,
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ):
        await asyncio.gather(
            *[booker._book_single(s, booking_won) for s in slots],
            return_exceptions=True,
        )

    assert click_count == 1, (
        f"Exactly one click under 5-way race; got {click_count}"
    )


@pytest.mark.asyncio
async def test_prep_failure_returns_false_without_clicking():
    """If _prepare_for_confirm returns False, the task aborts BEFORE
    even attempting to acquire the lock — no click."""
    booker = _make_booker()

    click_count = 0

    async def fake_prepare(page, slot, **_kwargs):
        return False  # CVC missing, payment never confirmed, etc.

    async def fake_click(page, slot):
        nonlocal click_count
        click_count += 1
        return True

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/checkout/abc"
    page.close = AsyncMock()
    booker.browser.new_page = AsyncMock(return_value=page)

    booking_won = asyncio.Event()
    slot = _make_slot("5:00 PM")

    with patch.object(
        booker, "_prepare_for_confirm", side_effect=fake_prepare
    ), patch.object(
        booker, "_execute_confirm_click_and_verify",
        side_effect=fake_click,
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ):
        result = await booker._book_single(slot, booking_won)

    assert result is False
    assert click_count == 0, "Click must not happen when prep fails"


@pytest.mark.asyncio
async def test_only_one_task_executes_confirm_click_under_lock():
    """Existing invariant preserved: even with split prep, the click
    itself runs under booker._confirm_lock — exactly one task wins.
    Tested via direct counter on the click side (concurrency above
    proves it's atomic)."""
    booker = _make_booker()

    # Spy the lock to confirm it's still held during the click
    real_lock = booker._confirm_lock
    lock_was_held_during_click = []

    async def fake_click(page, slot):
        lock_was_held_during_click.append(real_lock.locked())
        return True

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/checkout/abc"
    page.close = AsyncMock()
    booker.browser.new_page = AsyncMock(return_value=page)

    booking_won = asyncio.Event()
    slot = _make_slot("5:00 PM")

    with patch.object(
        booker, "_prepare_for_confirm", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_execute_confirm_click_and_verify",
        side_effect=fake_click,
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ):
        await booker._book_single(slot, booking_won)

    assert lock_was_held_during_click == [True], (
        f"_execute_confirm_click_and_verify must run while lock is held; "
        f"got {lock_was_held_during_click}"
    )
