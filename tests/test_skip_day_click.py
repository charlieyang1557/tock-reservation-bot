"""Tests for the skip_day_click_check config flag (Phase B1.5).

Hypothesis: Tock's `/search?date=YYYY-MM-DD` URL already selects that date
in the SPA, making the post-load `_click_day` call redundant. If true, we
can skip that click and save 50–300 ms per scan.

Default stays False this release. The flag is wired up but not flipped.
We A/B during a real release window before changing the default.

Behavior:
  - flag False (default): existing behavior — always click_day before collecting
  - flag True: skip click_day; if collection yields 0 slots, fall back to
    click_day and retry once (bounded fallback so a SPA quirk doesn't
    silently miss slots)
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_config(skip_day_click_check: bool = False) -> Config:
    return Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        skip_day_click_check=skip_day_click_check,
    )


def _make_checker(skip: bool = False):
    cfg = _make_config(skip_day_click_check=skip)
    browser = MagicMock()
    return AvailabilityChecker(cfg, browser, MagicMock())


def test_config_has_skip_day_click_check_field_defaulting_false():
    """The config carries the flag; the default keeps existing behavior."""
    cfg = _make_config()
    assert hasattr(cfg, "skip_day_click_check")
    assert cfg.skip_day_click_check is False


def test_config_can_set_skip_day_click_check_true():
    """The flag can be turned on without breaking anything else."""
    cfg = _make_config(skip_day_click_check=True)
    assert cfg.skip_day_click_check is True


# ---------------------------------------------------------------------------
# Checker behavior — _check_date
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_date_skip_day_click_finds_slots():
    """When the flag is True and slots are visible without a day click,
    _check_date returns them WITHOUT calling _click_day."""
    checker = _make_checker(skip=True)

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={"index": 0, "count": 1})
    checker.browser.new_page = AsyncMock(return_value=page)

    target_date = date(2026, 5, 15)
    expected_slots = [AvailableSlot(
        slot_date=target_date, slot_time="5:00 PM", day_of_week="Friday",
    )]

    with patch.object(
        checker, "_wait_for_calendar", AsyncMock(return_value=True)
    ), patch.object(
        checker, "_click_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        checker, "_collect_slots_multi", AsyncMock(return_value=expected_slots)
    ):
        slots = await checker._check_date(target_date)

    assert len(slots) == 1
    click_day_mock.assert_not_called(), (
        "Skip-mode must not call _click_day when slots are already visible "
        "via the SPA URL."
    )


@pytest.mark.asyncio
async def test_check_date_skip_day_click_falls_back_when_no_slots():
    """When the flag is True and the no-click attempt finds 0 slots,
    fall back to clicking the day and retry collection once."""
    checker = _make_checker(skip=True)

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={"index": 0, "count": 1})
    checker.browser.new_page = AsyncMock(return_value=page)

    target_date = date(2026, 5, 15)
    found_slots = [AvailableSlot(
        slot_date=target_date, slot_time="5:00 PM", day_of_week="Friday",
    )]
    # First call (no click): 0 slots. Second call (after click): 1 slot.
    collect_calls = [[], found_slots]
    collect_mock = AsyncMock(side_effect=collect_calls)

    with patch.object(
        checker, "_wait_for_calendar", AsyncMock(return_value=True)
    ), patch.object(
        checker, "_click_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        checker, "_collect_slots_multi", collect_mock
    ):
        slots = await checker._check_date(target_date)

    assert len(slots) == 1
    click_day_mock.assert_called_once(), (
        "Fallback must invoke _click_day when the no-click path yielded 0 slots."
    )
    # _collect_slots_multi called twice: pre-click (got 0), then post-click (got 1)
    assert collect_mock.await_count == 2


@pytest.mark.asyncio
async def test_check_date_default_false_still_clicks_day():
    """Default (flag False) keeps existing behavior — _click_day is always
    called before slot collection."""
    checker = _make_checker(skip=False)

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock(return_value={"index": 0, "count": 1})
    checker.browser.new_page = AsyncMock(return_value=page)

    target_date = date(2026, 5, 15)
    expected_slots = [AvailableSlot(
        slot_date=target_date, slot_time="5:00 PM", day_of_week="Friday",
    )]

    with patch.object(
        checker, "_wait_for_calendar", AsyncMock(return_value=True)
    ), patch.object(
        checker, "_click_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        checker, "_collect_slots_multi", AsyncMock(return_value=expected_slots)
    ):
        await checker._check_date(target_date)

    click_day_mock.assert_called_once(), (
        "Default behavior must continue to call _click_day exactly once."
    )


# ---------------------------------------------------------------------------
# Booker behavior — _book_single
# ---------------------------------------------------------------------------

def _make_booker_config(skip_day_click_check: bool = False) -> Config:
    return _make_config(skip_day_click_check=skip_day_click_check).__class__(
        **{**_make_config(skip_day_click_check=skip_day_click_check).__dict__,
           "dry_run": False}
    )


@pytest.mark.asyncio
async def test_book_single_skip_day_click_then_clicks_slot():
    """When the flag is True and _click_time_slot succeeds without
    _click_calendar_day, the booker proceeds straight to checkout."""
    from src.booker import TockBooker
    import asyncio

    cfg = _make_booker_config(skip_day_click_check=True)
    browser = MagicMock()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    booker = TockBooker(cfg, browser, MagicMock())
    slot = AvailableSlot(
        slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday",
    )

    with patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_confirm_booking", AsyncMock(return_value=True)
    ):
        await booker._book_single(slot, asyncio.Event())

    click_day_mock.assert_not_called(), (
        "Skip-mode must not call _click_calendar_day when the slot button "
        "is already visible from the SPA URL."
    )


@pytest.mark.asyncio
async def test_book_single_skip_day_click_falls_back_when_no_slot_buttons():
    """When the flag is True and _click_time_slot returns False on the
    first attempt, the booker falls back to clicking the day and retries."""
    from src.booker import TockBooker
    import asyncio

    cfg = _make_booker_config(skip_day_click_check=True)
    browser = MagicMock()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    booker = TockBooker(cfg, browser, MagicMock())
    slot = AvailableSlot(
        slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday",
    )
    # First click_time_slot returns False, second returns True after the day
    # click fallback fires
    click_time_slot_mock = AsyncMock(side_effect=[False, True])

    with patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        booker, "_click_time_slot", click_time_slot_mock
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_confirm_booking", AsyncMock(return_value=True)
    ):
        result = await booker._book_single(slot, asyncio.Event())

    assert result is True
    click_day_mock.assert_called_once(), (
        "Fallback must invoke _click_calendar_day when the first slot-click "
        "attempt returned no clickable buttons."
    )
    assert click_time_slot_mock.await_count == 2, (
        "Slot-click must be retried after the day-click fallback fires."
    )


@pytest.mark.asyncio
async def test_book_single_default_false_still_clicks_day():
    """Default behavior preserved: _click_calendar_day is always called
    when the booker owns the page (no warm page handoff)."""
    from src.booker import TockBooker
    import asyncio

    cfg = _make_booker_config(skip_day_click_check=False)
    browser = MagicMock()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search?date=2026-05-15"
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.evaluate = AsyncMock()
    browser.new_page = AsyncMock(return_value=page)

    booker = TockBooker(cfg, browser, MagicMock())
    slot = AvailableSlot(
        slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday",
    )

    with patch.object(
        booker, "_wait_for_selector", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_click_calendar_day", AsyncMock(return_value=True)
    ) as click_day_mock, patch.object(
        booker, "_click_time_slot", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_wait_for_checkout", AsyncMock(return_value=True)
    ), patch.object(
        booker, "_confirm_booking", AsyncMock(return_value=True)
    ):
        await booker._book_single(slot, asyncio.Event())

    click_day_mock.assert_called_once(), (
        "Default behavior must continue to call _click_calendar_day."
    )
