"""Tests for time-slot matching in booker._click_time_slot()."""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from src.checker import AvailableSlot
from src.booker import TockBooker
from src.config import Config


def _make_config() -> Config:
    return Config(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )


def _make_slot(time_str: str = "5:00 PM") -> AvailableSlot:
    return AvailableSlot(slot_date=date(2026, 4, 17), slot_time=time_str, day_of_week="Friday")


class TestClickTimeSlot:
    @pytest.mark.asyncio
    async def test_clicks_matching_time_not_first(self):
        """When target is '8:00 PM' but '5:00 PM' is first, click '8:00 PM'."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("8:00 PM")

        btn_5pm = AsyncMock()
        btn_5pm.text_content = AsyncMock(return_value="5:00 PM\nBook")
        btn_8pm = AsyncMock()
        btn_8pm.text_content = AsyncMock(return_value="8:00 PM\nBook")
        btn_8pm.click = AsyncMock()

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=2)
        locator_mock.nth = MagicMock(side_effect=lambda i: [btn_5pm, btn_8pm][i])
        page.locator = MagicMock(return_value=locator_mock)
        page.wait_for_selector = AsyncMock(return_value=True)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn_8pm.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_falls_back_to_first_when_no_match(self):
        """If no button matches, click first button with warning."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("9:00 PM")

        btn = AsyncMock()
        btn.text_content = AsyncMock(return_value="5:00 PM\nBook")
        btn.click = AsyncMock()

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=1)
        locator_mock.nth = MagicMock(return_value=btn)
        page.locator = MagicMock(return_value=locator_mock)
        page.wait_for_selector = AsyncMock(return_value=True)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self):
        """Match should be case-insensitive."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("5:00 pm")  # lowercase

        btn = AsyncMock()
        btn.text_content = AsyncMock(return_value="5:00 PM\nBook")
        btn.click = AsyncMock()

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=1)
        locator_mock.nth = MagicMock(return_value=btn)
        page.locator = MagicMock(return_value=locator_mock)
        page.wait_for_selector = AsyncMock(return_value=True)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn.click.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_buttons_returns_false(self):
        """If no slot buttons are found at all, return False."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("5:00 PM")

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=0)
        page.locator = MagicMock(return_value=locator_mock)
        page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

        result = await booker._click_time_slot(page, slot)
        assert result is False

    @pytest.mark.asyncio
    async def test_regex_time_match(self):
        """Match via regex extraction when text has extra content."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("7:30 PM")

        btn_other = AsyncMock()
        btn_other.text_content = AsyncMock(return_value="Dinner Experience\n$250")
        btn_target = AsyncMock()
        btn_target.text_content = AsyncMock(return_value="Dinner Experience 7:30 PM\n$250")
        btn_target.click = AsyncMock()

        page = AsyncMock()
        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=2)
        locator_mock.nth = MagicMock(side_effect=lambda i: [btn_other, btn_target][i])
        page.locator = MagicMock(return_value=locator_mock)
        page.wait_for_selector = AsyncMock(return_value=True)

        result = await booker._click_time_slot(page, slot)
        assert result is True
        btn_target.click.assert_awaited_once()
