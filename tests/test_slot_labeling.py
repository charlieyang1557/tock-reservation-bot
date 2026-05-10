"""Tests for slot-label extraction priority (A3 fix).

The JS in src/checker.py::_COLLECT_SLOTS_JS extracts a real time string
in priority order:
  1. Child span (existing slot_time_text selector)
  2. Time pattern in parent.text_content()
  3. Time pattern in any ancestor up to 3 levels
  4. Button's aria-label or title attribute
  5. Button's own text content (if not bare "Book")

If none of the above yield a parseable time, the slot must NOT be
emitted. The "Slot N" fallback is removed (Apr 17 root cause).

Post-B1.3: these tests assert the WRAPPER faithfully translates the JS
{time, source} entries into AvailableSlot objects (or drops them). The
JS extraction algorithm is reviewed inline in src/checker.py and
exercised end-to-end via --test-booking-flow.
"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker


def _make_checker():
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
    tracker = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


def _page_returning(slot_dicts):
    """Build a page mock whose evaluate returns the given slots list."""
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True,
        "button_count": len(slot_dicts),
        "slots": slot_dicts,
    })
    return page


@pytest.mark.asyncio
async def test_extracts_from_child_span():
    """Source 1: JS reports source=1 (child span) → wrapper emits 5:00 PM."""
    checker = _make_checker()
    page = _page_returning([{"time": "5:00 PM", "source": 1}])

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), "button.Consumer-resultsListItem.is-available"
    )
    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_extracts_from_aria_label():
    """Source 4: JS extracted from aria-label → wrapper emits the time."""
    checker = _make_checker()
    page = _page_returning([{"time": "5:30 PM", "source": 4}])

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button.Consumer-resultsListItem.is-available'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "5:30 PM"


@pytest.mark.asyncio
async def test_extracts_from_grandparent():
    """Source 3: JS extracted from a 2-level-up ancestor → wrapper emits."""
    checker = _make_checker()
    page = _page_returning([{"time": "6:00 PM", "source": 3}])

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button.Consumer-resultsListItem.is-available'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "6:00 PM"


@pytest.mark.asyncio
async def test_no_time_anywhere_drops_slot():
    """If JS reports time=None for an entry, the wrapper drops it.
    The 'Slot N' fallback is forbidden (Apr 17 root cause)."""
    checker = _make_checker()
    page = _page_returning([{"time": None, "source": -1}])

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button.Consumer-resultsListItem.is-available'
    )
    assert slots == [], f"Expected no slot when time cannot be extracted; got {slots}"


@pytest.mark.asyncio
async def test_no_slot_n_label_in_output():
    """Regression: 'Slot 1', 'Slot 2', etc. must never appear in slot_time."""
    checker = _make_checker()
    # 3 buttons, none with extractable time
    page = _page_returning([
        {"time": None, "source": -1},
        {"time": None, "source": -1},
        {"time": None, "source": -1},
    ])

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button.Consumer-resultsListItem.is-available'
    )
    # Primary assertion: with no extractable time on any button, no slots emit.
    # The "Slot N" fallback would have produced 3 slots — its absence is a 0-len list.
    assert slots == [], (
        f"Expected no slots when no time is extractable; got {slots}"
    )
    # Defense in depth: even if a future regression emits placeholder slots,
    # they must never start with 'slot' or 'slot' (with or without space).
    for s in slots:
        assert "slot" not in s.slot_time.lower(), (
            f"Placeholder label leaked: {s.slot_time!r}"
        )
