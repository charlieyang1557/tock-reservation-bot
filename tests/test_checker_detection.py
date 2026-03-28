"""Tests for the rewritten slot detection flow in AvailabilityChecker.

Covers:
  - _click_day uses all_day_button (not filtered by is-available)
  - _check_date bypasses is-available gate
  - _check_date detects "Book now" button and clicks it
"""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker
from src.config import Config


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


def _make_mock_button(text: str, disabled: bool = False):
    """Create a mock Playwright ElementHandle for a calendar day button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.click = AsyncMock()
    btn.get_attribute = AsyncMock(side_effect=lambda attr: (
        "true" if attr == "disabled" and disabled
        else "ConsumerCalendar-day is-in-month" if attr == "class"
        else None
    ))
    btn.is_disabled = AsyncMock(return_value=disabled)
    return btn


# ---------------------------------------------------------------------------
# _click_day: uses all_day_button, not available_day_button
# ---------------------------------------------------------------------------

class TestClickDayUsesAllButtons:
    """_click_day should find and click the target day by number
    using all_day_button (not filtered by is-available)."""

    @pytest.mark.asyncio
    async def test_clicks_matching_day_number(self):
        """Should click button with matching day number text."""
        checker = _make_checker()
        page = AsyncMock()

        btn3 = _make_mock_button("3")
        btn4 = _make_mock_button("4")
        btn5 = _make_mock_button("5")
        page.query_selector_all = AsyncMock(return_value=[btn3, btn4, btn5])

        target = date(2026, 4, 4)
        result = await checker._click_day(page, target)

        assert result is True
        btn4.click.assert_called_once()
        btn3.click.assert_not_called()
        btn5.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_day_not_found(self):
        """If target day number is not in any button, return False."""
        checker = _make_checker()
        page = AsyncMock()

        btn1 = _make_mock_button("1")
        btn2 = _make_mock_button("2")
        page.query_selector_all = AsyncMock(return_value=[btn1, btn2])

        target = date(2026, 4, 15)
        result = await checker._click_day(page, target)

        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_when_no_buttons(self):
        """No calendar day buttons at all → False."""
        checker = _make_checker()
        page = AsyncMock()
        page.query_selector_all = AsyncMock(return_value=[])

        target = date(2026, 4, 4)
        result = await checker._click_day(page, target)

        assert result is False


# ---------------------------------------------------------------------------
# _check_date: bypasses is-available, uses click-by-number + Book Now
# ---------------------------------------------------------------------------

class TestCheckDateBypassesIsAvailable:
    """_check_date should NOT gate on is-available class.
    It should click the day by number and check for slots or Book Now."""

    @pytest.mark.asyncio
    async def test_finds_slots_after_clicking_day_without_is_available(self):
        """Day has no is-available class but slots exist after clicking → detected."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        # Day button (no is-available class — just is-in-month)
        btn4 = _make_mock_button("4")

        # Slot button that appears after clicking the day
        slot_btn = AsyncMock()
        time_span = AsyncMock()
        time_span.text_content = AsyncMock(return_value="5:00 PM")
        slot_btn.query_selector = AsyncMock(return_value=time_span)

        # Track which selectors are queried
        query_calls = []

        async def mock_query_all(selector):
            query_calls.append(selector)
            if "resultsListItem" in selector:
                # Pre-loaded: empty. After day click: has slot.
                # Count how many times resultsListItem was queried
                results_count = sum(1 for s in query_calls if "resultsListItem" in s)
                if results_count >= 2:  # after day click
                    return [slot_btn]
                return []
            if "ConsumerCalendar-day" in selector:
                return [btn4]
            return []

        page.query_selector_all = mock_query_all
        page.query_selector = AsyncMock(return_value=None)  # no Book Now
        page.screenshot = AsyncMock()

        # wait_for_selector: calendar OK, others may timeout
        async def mock_wait(selector, **kwargs):
            if "ConsumerCalendar-month" in selector:
                return True
            raise TimeoutError("timeout")

        page.wait_for_selector = mock_wait

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert len(slots) == 1
        assert slots[0].slot_time == "5:00 PM"
        assert slots[0].slot_date == target
        # Verify the day was clicked
        btn4.click.assert_called()

    @pytest.mark.asyncio
    async def test_clicks_book_now_when_no_direct_slots(self):
        """After clicking day, no slot buttons but Book Now visible → click it."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        btn4 = _make_mock_button("4")
        book_now_btn = AsyncMock()

        slot_btn = AsyncMock()
        time_span = AsyncMock()
        time_span.text_content = AsyncMock(return_value="8:00 PM")
        slot_btn.query_selector = AsyncMock(return_value=time_span)

        query_calls = []

        async def mock_query_all(selector):
            query_calls.append(selector)
            if "resultsListItem" in selector:
                # Only return slots after Book Now has been clicked
                if book_now_btn.click.called:
                    return [slot_btn]
                return []
            if "ConsumerCalendar-day" in selector:
                return [btn4]
            return []

        page.query_selector_all = mock_query_all
        # query_selector for Book Now button
        page.query_selector = AsyncMock(return_value=book_now_btn)
        page.screenshot = AsyncMock()

        async def mock_wait(selector, **kwargs):
            if "ConsumerCalendar-month" in selector:
                return True
            if "resultsListItem" in selector and book_now_btn.click.called:
                return slot_btn
            raise TimeoutError("timeout")

        page.wait_for_selector = mock_wait

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert len(slots) >= 1
        assert slots[0].slot_time == "8:00 PM"
        book_now_btn.click.assert_called()

    @pytest.mark.asyncio
    async def test_returns_empty_when_day_click_fails(self):
        """If the target day can't be clicked (not in calendar), return []."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        # No buttons at all
        page.query_selector_all = AsyncMock(return_value=[])
        page.query_selector = AsyncMock(return_value=None)
        page.screenshot = AsyncMock()

        async def mock_wait(selector, **kwargs):
            if "ConsumerCalendar-month" in selector:
                return True
            raise TimeoutError("timeout")

        page.wait_for_selector = mock_wait

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert slots == []

    @pytest.mark.asyncio
    async def test_returns_empty_when_no_slots_and_no_book_now(self):
        """Day clicked but no slots and no Book Now → []."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        btn4 = _make_mock_button("4")

        async def mock_query_all(selector):
            if "ConsumerCalendar-day" in selector:
                return [btn4]
            return []  # no slots ever

        page.query_selector_all = mock_query_all
        page.query_selector = AsyncMock(return_value=None)  # no Book Now
        page.screenshot = AsyncMock()

        async def mock_wait(selector, **kwargs):
            if "ConsumerCalendar-month" in selector:
                return True
            raise TimeoutError("timeout")

        page.wait_for_selector = mock_wait

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert slots == []
