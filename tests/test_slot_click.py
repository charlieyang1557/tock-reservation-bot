"""Tests for time-slot matching in booker._click_time_slot().

Post-B1.2: the per-button match/click is one page.evaluate call. These
tests mock that evaluate response and assert the wrapper handles each
JS outcome correctly. The actual JS algorithm is reviewed inline in
src/booker.py and exercised end-to-end via --test-booking-flow."""
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


def _make_page_with_eval(matched_count: int, evaluate_result: dict):
    """Build a page where the first slot selector matches `matched_count`
    elements and page.evaluate returns `evaluate_result`."""
    page = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=True)
    page.evaluate = AsyncMock(return_value=evaluate_result)

    locator_mock = MagicMock()
    locator_mock.count = AsyncMock(return_value=matched_count)
    page.locator = MagicMock(return_value=locator_mock)
    return page


class TestClickTimeSlot:
    @pytest.mark.asyncio
    async def test_clicks_matching_time_not_first(self):
        """When target is '8:00 PM' but '5:00 PM' is first, JS must report
        an exact match on '8:00 PM' (not a fallback to the first button)."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("8:00 PM")
        # JS does the iteration and reports exact match on the 2nd button
        page = _make_page_with_eval(
            matched_count=2,
            evaluate_result={"clicked": True, "text": "8:00 PM\nBook", "reason": "exact"},
        )

        result = await booker._click_time_slot(page, slot)
        assert result is True
        # The wrapper passed the right target time; JS returned a non-fallback reason
        args, _ = page.evaluate.call_args
        js_arg = args[1] if len(args) > 1 else args[0]
        assert js_arg["targetTime"] == "8:00 PM"

    @pytest.mark.asyncio
    async def test_falls_back_to_first_when_no_match(self):
        """If no button matches and not strict, JS reports first-fallback
        clicked → wrapper returns True."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("9:00 PM")
        page = _make_page_with_eval(
            matched_count=1,
            evaluate_result={
                "clicked": True,
                "text": "5:00 PM\nBook",
                "reason": "first-fallback",
            },
        )

        result = await booker._click_time_slot(page, slot)
        assert result is True

    @pytest.mark.asyncio
    async def test_case_insensitive_match(self):
        """JS does .toUpperCase() on both sides; wrapper trusts the result.

        We assert the wrapper passes the UPPERCASED target_time to JS so
        the JS-side comparison always matches case-correctly."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("5:00 pm")  # lowercase input
        page = _make_page_with_eval(
            matched_count=1,
            evaluate_result={"clicked": True, "text": "5:00 PM\nBook", "reason": "exact"},
        )

        result = await booker._click_time_slot(page, slot)
        assert result is True
        args, _ = page.evaluate.call_args
        js_arg = args[1] if len(args) > 1 else args[0]
        assert js_arg["targetTime"] == "5:00 PM"  # uppercased

    @pytest.mark.asyncio
    async def test_no_buttons_returns_false(self):
        """If no slot buttons are found, wrapper short-circuits without
        calling page.evaluate at all."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("5:00 PM")

        page = AsyncMock()
        page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
        page.evaluate = AsyncMock()  # should NOT be called

        locator_mock = MagicMock()
        locator_mock.count = AsyncMock(return_value=0)
        page.locator = MagicMock(return_value=locator_mock)

        result = await booker._click_time_slot(page, slot)
        assert result is False
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_regex_time_match(self):
        """JS reports a regex (not exact substring) match → wrapper returns True."""
        booker = TockBooker(_make_config(), MagicMock(), MagicMock())
        slot = _make_slot("7:30 PM")
        page = _make_page_with_eval(
            matched_count=2,
            evaluate_result={
                "clicked": True,
                "text": "Dinner Experience 7:30 PM\n$250",
                "reason": "regex",
            },
        )

        result = await booker._click_time_slot(page, slot)
        assert result is True
