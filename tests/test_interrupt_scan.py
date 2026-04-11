"""Tests for first-slot interrupt during concurrent sniper scanning."""
import asyncio
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_checker():
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[], preferred_time="17:00", scan_weeks=4,
        dry_run=True, headless=True, sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", debug_screenshots=False,
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record_deferred = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


@pytest.mark.asyncio
async def test_abort_event_passed_in_sniper_concurrent_mode():
    """In sniper concurrent mode, _check_date receives a non-None abort_event."""
    checker = _make_checker()
    received_events = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        received_events.append(abort_event)
        return []

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=90.0
        )

    assert len(received_events) > 0
    assert all(e is not None for e in received_events), (
        "abort_event must be non-None in sniper concurrent mode"
    )


@pytest.mark.asyncio
async def test_remaining_tasks_see_abort_event_after_first_slot():
    """After one date finds a slot and sets the event, others see it and return []."""
    checker = _make_checker()
    first_call = [True]

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        if abort_event is not None and abort_event.is_set():
            return []
        slot = AvailableSlot(
            slot_date=target_date,
            slot_time="5:00 PM",
            day_of_week=target_date.strftime("%A"),
        )
        if abort_event is not None:
            abort_event.set()
        return [slot]

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=90.0
        )

    assert len(result) >= 1
    assert result[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_sequential_sniper_stops_after_first_slot():
    """In sequential sniper mode, scanning stops after first date with slots."""
    checker = _make_checker()
    dates_scanned = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        dates_scanned.append(target_date.isoformat())
        return [AvailableSlot(
            slot_date=target_date, slot_time="5:00 PM",
            day_of_week=target_date.strftime("%A"),
        )]

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=False, keep_pages=True, sniper_window_age_sec=90.0
        )

    assert len(dates_scanned) == 1
    assert len(result) == 1


@pytest.mark.asyncio
async def test_non_sniper_concurrent_no_abort_event():
    """Non-sniper concurrent scans (keep_pages=False) must NOT receive abort_event."""
    checker = _make_checker()
    received_events = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        received_events.append(abort_event)
        return []

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=False, sniper_window_age_sec=0.0
        )

    assert all(e is None for e in received_events), (
        "abort_event must be None in non-sniper mode"
    )
