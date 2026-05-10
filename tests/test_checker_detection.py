"""Tests for the slot detection flow in AvailabilityChecker.

Covers:
  - _click_day uses all_day_button (not filtered by is-available)
  - _check_date multi-selector fallback (same as --test-booking-flow)
  - _collect_slots_multi extracts time from various DOM patterns
"""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker
from src.config import Config
from tests.conftest import make_page_locator


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_checker(**config_overrides) -> AvailabilityChecker:
    cfg = _make_config(**config_overrides)
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(cfg, browser, tracker)


def _make_mock_button(text: str):
    """Create a mock Playwright ElementHandle for a calendar day button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.click = AsyncMock()
    btn.get_attribute = AsyncMock(return_value="ConsumerCalendar-day is-in-month")
    return btn


# ---------------------------------------------------------------------------
# _click_day: uses all_day_button, not available_day_button
# ---------------------------------------------------------------------------

class TestClickDayUsesAllButtons:
    """_click_day now uses page.evaluate() for a single browser round-trip."""

    @pytest.mark.asyncio
    async def test_clicks_matching_day_number(self):
        checker = _make_checker()
        page = AsyncMock()
        # page.evaluate returns True when JS finds and clicks the matching day
        page.evaluate = AsyncMock(return_value=True)

        result = await checker._click_day(page, date(2026, 4, 4))

        assert result is True
        page.evaluate.assert_called_once()
        # Verify the target day number "4" was passed as argument
        call_args = page.evaluate.call_args
        assert call_args[0][1] == ["button.ConsumerCalendar-day.is-in-month", "4"]

    @pytest.mark.asyncio
    async def test_returns_false_when_day_not_found(self):
        checker = _make_checker()
        page = AsyncMock()
        # page.evaluate returns False when no matching day button found
        page.evaluate = AsyncMock(return_value=False)

        assert await checker._click_day(page, date(2026, 4, 15)) is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_buttons(self):
        checker = _make_checker()
        page = AsyncMock()
        # page.evaluate returns False when no buttons exist at all
        page.evaluate = AsyncMock(return_value=False)

        assert await checker._click_day(page, date(2026, 4, 4)) is False


# ---------------------------------------------------------------------------
# _collect_slots_multi: extracts time from various DOM patterns
# ---------------------------------------------------------------------------

class TestCollectSlotsMulti:

    @pytest.mark.asyncio
    async def test_extracts_time_from_parent_text(self):
        """Source 2: when JS reports a parent-text extraction, wrapper
        builds an AvailableSlot with that time."""
        checker = _make_checker()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "container_used": True,
            "button_count": 1,
            "slots": [{"time": "5:00 PM", "source": 2}],
        })

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button.Consumer-resultsListItem.is-available'
        )

        assert len(slots) == 1
        assert slots[0].slot_time == "5:00 PM"

    @pytest.mark.asyncio
    async def test_drops_slot_when_no_time_found(self):
        """A3 fix: when JS reports time=None for an entry, the wrapper
        drops it. The 'Slot N' fallback was removed (Apr 17 root cause)."""
        checker = _make_checker()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "container_used": True,
            "button_count": 1,
            "slots": [{"time": None, "source": -1}],
        })

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button.Consumer-resultsListItem.is-available'
        )

        assert slots == [], (
            f"Slot with no extractable time must be dropped; got {slots}"
        )

    @pytest.mark.asyncio
    async def test_multiple_slots(self):
        """Two button entries from JS → two slots from wrapper."""
        checker = _make_checker()
        page = AsyncMock()
        page.evaluate = AsyncMock(return_value={
            "container_used": True,
            "button_count": 2,
            "slots": [
                {"time": "5:00 PM", "source": 2},
                {"time": "8:00 PM", "source": 2},
            ],
        })

        slots = await checker._collect_slots_multi(
            page, date(2026, 4, 4), 'button.Consumer-resultsListItem.is-available'
        )

        assert len(slots) == 2
        assert slots[0].slot_time == "5:00 PM"
        assert slots[1].slot_time == "8:00 PM"
