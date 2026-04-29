"""Tests for racing all detected slots concurrently (Phase A+2 Task 1).

Today book_best_slot_race collapses to one slot per date via _best_per_date.
After this change, ALL slots are raced — multiple times on the same date
attempt concurrently. The existing asyncio.Lock + Event prevents the rare
double-booking, while maximizing hit rate (5pm AND 8pm of the same Friday
both attempted instead of just 5pm).

Also covers Fix 1 from Codex adversarial review: confirm-uncertainty kills
the race (prevents sequential double-booking on verification failure).
"""
import asyncio
from datetime import date
from unittest.mock import MagicMock, patch
import pytest

from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


@pytest.mark.asyncio
async def test_races_all_slots_including_same_date():
    """Multiple slots on the same date both result in booking attempts."""
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 2), slot_time="5:00 PM", day_of_week="Saturday"),
    ]

    attempted = []

    async def fake_book_single(slot, booking_won, **_kwargs):
        attempted.append(slot)
        if not booking_won.is_set():
            booking_won.set()
            return True
        return False

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        booked = await booker.book_best_slot_race(slots)

    # All 3 slots must be attempted (concurrent race)
    attempted_keys = {(s.slot_date_str, s.slot_time) for s in attempted}
    assert ("2026-05-01", "5:00 PM") in attempted_keys
    assert ("2026-05-01", "8:00 PM") in attempted_keys
    assert ("2026-05-02", "5:00 PM") in attempted_keys
    assert len(attempted) == 3
    assert booked is not None  # one winner


@pytest.mark.asyncio
async def test_lock_still_prevents_double_confirm():
    """Even with all slots racing, only ONE actually wins (lock invariant holds)."""
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
    ]

    winners = []

    async def fake_book_single(slot, booking_won, **_kwargs):
        await asyncio.sleep(0)  # yield to other tasks
        if booking_won.is_set():
            return False  # respects the event
        booking_won.set()
        winners.append(slot)
        return True

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        await booker.book_best_slot_race(slots)

    assert len(winners) == 1, (
        f"Lock+event must serialize confirm to exactly one winner; got {winners}"
    )


@pytest.mark.asyncio
async def test_warm_page_claimed_by_only_one_task():
    """When 2 slots on the same date race, the warm page is given to exactly
    one task; the other falls through to a fresh page.

    This prevents DOM state corruption that happened pre-fix when both tasks
    clicked slot buttons on the same shared Page object.
    """
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
    ]

    # Sentinel object — both tasks must NOT receive the same instance
    warm_page_sentinel = MagicMock(name="warm_page_for_2026_05_01")
    warm_pages = {"2026-05-01": warm_page_sentinel}

    received_warm_pages = []

    async def fake_book_single(slot, booking_won, warm_page=None):
        received_warm_pages.append(warm_page)
        await asyncio.sleep(0)  # yield so both tasks have a chance to read warm_pages
        if not booking_won.is_set():
            booking_won.set()
            return True
        return False

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        # MagicMock duck-types as Page in the mock chain — runtime is fine,
        # but Pyright's dict invariance flags the value type.
        await booker.book_best_slot_race(slots, warm_pages=warm_pages)  # type: ignore[arg-type]

    # Exactly one task got the warm page; the other got None (fresh page path)
    sentinel_count = received_warm_pages.count(warm_page_sentinel)
    none_count = received_warm_pages.count(None)
    assert sentinel_count == 1, (
        f"Expected exactly 1 task to claim the warm page; got {sentinel_count} "
        f"(received: {received_warm_pages})"
    )
    assert none_count == 1, (
        f"Expected exactly 1 task to fall through to fresh page (None); got {none_count}"
    )
    # warm_pages dict was emptied by .pop() — the same date_str shouldn't be there anymore
    assert "2026-05-01" not in warm_pages


@pytest.mark.asyncio
async def test_confirm_uncertainty_kills_race():
    """If task A's _confirm_booking returns False AFTER clicking confirm,
    no further task may attempt confirm — Tock may have silently accepted."""
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
    ]

    # Track which tasks reached the confirm phase
    confirm_attempts = []

    async def fake_book_single(slot, booking_won, **_kwargs):
        # Simulate the real flow: acquire lock, check gates, then "click confirm"
        async with booker._confirm_lock:
            if booking_won.is_set():
                return False
            if booker._confirm_attempted.is_set():
                # Second task SEES the attempted-flag and aborts
                return False
            booker._confirm_attempted.set()
            confirm_attempts.append(slot)
            await asyncio.sleep(0)  # simulate the confirm click
            # Simulate verification failure on the first attempt — but still
            # soft-win so the race is killed
            booking_won.set()
            return False

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        await booker.book_best_slot_race(slots)

    # Exactly ONE task got to "click confirm" — even though the first one
    # returned False. The second was blocked by _confirm_attempted.
    assert len(confirm_attempts) == 1, (
        f"Expected exactly 1 confirm attempt; got {len(confirm_attempts)} "
        f"(double-booking risk if >1)"
    )


@pytest.mark.asyncio
async def test_confirm_attempted_resets_per_race():
    """Each call to book_best_slot_race starts with a clean _confirm_attempted."""
    booker = _make_booker()
    booker._confirm_attempted.set()  # simulate stale state from prior race

    slots = [AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday")]

    async def fake_book_single(slot, booking_won, **_kwargs):
        # If we get here, the gate was cleared
        assert not booker._confirm_attempted.is_set(), (
            "Stale _confirm_attempted from prior race must be cleared"
        )
        booking_won.set()
        return True

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        result = await booker.book_best_slot_race(slots)

    assert result is not None
