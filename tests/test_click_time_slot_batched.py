"""Tests for the single-evaluate _click_time_slot (Phase B1.2).

The new implementation does the per-button text/click iteration inside one
`page.evaluate` round-trip instead of N Python↔browser hops. This file
tests the Python wrapper that:

  1. Discovers the first selector with matching elements (locator.count loop)
  2. Calls page.evaluate(JS, {selector, targetTime, slotTimeRaw,
     isGeneric, strictTimeMatch}) — single round-trip
  3. Reads {clicked: bool, text: str | None, reason: str} from the result
  4. Logs + returns clicked

The JS algorithm is verified against the existing test_slot_click.py
behavioural tests (which were updated to mock page.evaluate's return).
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

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
    return TockBooker(config, MagicMock(), MagicMock())


def _make_slot(slot_time: str = "5:00 PM"):
    return AvailableSlot(
        slot_date=date(2026, 5, 15),
        slot_time=slot_time,
        day_of_week="Friday",
    )


def _make_page(matched_count: int, evaluate_result: dict):
    """Build a page mock where the first slot selector reports `matched_count`
    elements and `page.evaluate` returns `evaluate_result`."""
    page = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=True)
    page.evaluate = AsyncMock(return_value=evaluate_result)

    locator_mock = MagicMock()
    locator_mock.count = AsyncMock(return_value=matched_count)
    page.locator = MagicMock(return_value=locator_mock)
    return page


@pytest.mark.asyncio
async def test_click_time_slot_clicks_exact_time_match():
    """JS reports an exact-text match → wrapper returns True."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = _make_page(
        matched_count=2,
        evaluate_result={"clicked": True, "text": "5:00 PM", "reason": "exact"},
    )

    result = await booker._click_time_slot(page, slot)
    assert result is True
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_time_slot_clicks_regex_time_match():
    """JS reports a regex match (button text has surrounding cruft) → True."""
    booker = _make_booker()
    slot = _make_slot("7:30 PM")
    page = _make_page(
        matched_count=1,
        evaluate_result={
            "clicked": True,
            "text": "Dinner Experience 7:30 PM\n$250",
            "reason": "regex",
        },
    )

    result = await booker._click_time_slot(page, slot)
    assert result is True


@pytest.mark.asyncio
async def test_click_time_slot_clicks_generic_button_when_parent_has_time():
    """JS reports the parent of a generic 'Book' button contained the
    target time → wrapper returns True."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = _make_page(
        matched_count=1,
        evaluate_result={
            "clicked": True,
            "text": "5:00 PM  Book  2 guests",
            "reason": "generic-parent",
        },
    )

    result = await booker._click_time_slot(page, slot)
    assert result is True


@pytest.mark.asyncio
async def test_click_time_slot_skips_generic_button_when_parent_lacks_time():
    """JS reports no candidate clicked (only generics whose parents lack
    the target time) → wrapper returns False without raising."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = _make_page(
        matched_count=1,
        evaluate_result={"clicked": False, "text": None, "reason": "no-match"},
    )

    result = await booker._click_time_slot(page, slot)
    assert result is False


@pytest.mark.asyncio
async def test_click_time_slot_strict_mode_refuses_fallback():
    """When strict_time_match=True and JS reports the strict-refused
    fallback, wrapper returns False (no rescue click)."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = _make_page(
        matched_count=2,
        evaluate_result={
            "clicked": False,
            "text": "8:00 PM",
            "reason": "strict-refused-fallback",
        },
    )

    result = await booker._click_time_slot(page, slot, strict_time_match=True)
    assert result is False
    # The wrapper must have told the JS that strict mode is on
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    assert js_arg["strictTimeMatch"] is True


@pytest.mark.asyncio
async def test_click_time_slot_non_strict_clicks_first_specific_button():
    """When strict_time_match=False (default) and JS reports first-fallback
    clicked, wrapper returns True (existing legacy behavior preserved)."""
    booker = _make_booker()
    slot = _make_slot("9:00 PM")
    page = _make_page(
        matched_count=1,
        evaluate_result={
            "clicked": True,
            "text": "5:00 PM\nBook",
            "reason": "first-fallback",
        },
    )

    result = await booker._click_time_slot(page, slot)
    assert result is True
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    assert js_arg["strictTimeMatch"] is False


@pytest.mark.asyncio
async def test_click_time_slot_passes_target_time_in_args():
    """Sanity check: the JS gets the target_time, raw slot_time, and the
    selected matched_selector."""
    booker = _make_booker()
    slot = _make_slot("8:00 PM")
    page = _make_page(
        matched_count=1,
        evaluate_result={"clicked": True, "text": "8:00 PM", "reason": "exact"},
    )

    await booker._click_time_slot(page, slot)
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    # The JS contract — these keys must always be present
    assert js_arg["targetTime"] == "8:00 PM"
    assert js_arg["slotTimeRaw"] == "8:00 PM"
    assert "selector" in js_arg
    assert "isGeneric" in js_arg
    assert "strictTimeMatch" in js_arg


@pytest.mark.asyncio
async def test_click_time_slot_returns_false_when_no_buttons_found():
    """If no selector matches any elements, wrapper returns False
    without attempting an evaluate call."""
    booker = _make_booker()
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
async def test_click_time_slot_only_one_evaluate_round_trip():
    """The whole iteration is one JS round-trip — no per-button awaits."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = _make_page(
        matched_count=10,  # 10 buttons would be 10 round-trips on the old impl
        evaluate_result={"clicked": True, "text": "5:00 PM", "reason": "exact"},
    )

    await booker._click_time_slot(page, slot)
    assert page.evaluate.await_count == 1, (
        f"Expected exactly one page.evaluate call; got {page.evaluate.await_count}"
    )
