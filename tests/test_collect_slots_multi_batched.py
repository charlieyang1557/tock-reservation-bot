"""Tests for the single-evaluate _collect_slots_multi (Phase B1.3).

Old behavior: per-button locator chain — page.locator(matched_selector)
.nth(i) → 5-source extraction with locator(`..`).text_content awaits at
each level. N buttons × ≤5 sources → up to ~5N round-trips per date.

New behavior: one page.evaluate that does the container scope check
AND the 5-source extraction inline, returning
  {container_used: bool, button_count: int,
   slots: [{time: str | null, source: int}, ...]}

The Python wrapper:
  - Translates each entry to AvailableSlot
  - Drops entries with time=None (preserves Apr 17 lesson: never fabricate "Slot N")
  - Logs the container fallback when container_used=False

The actual JS algorithm is reviewed inline in src/checker.py and
exercised end-to-end via --test-booking-flow.
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailabilityChecker
from src.config import Config


def _make_config() -> Config:
    return Config(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )


def _make_checker():
    return AvailabilityChecker(_make_config(), MagicMock(), MagicMock())


def _make_page(evaluate_result):
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value=evaluate_result)
    return page


@pytest.mark.asyncio
async def test_collect_slots_multi_single_evaluate_call():
    """One page.evaluate per call — not one per button."""
    checker = _make_checker()
    page = _make_page({
        "container_used": True,
        "button_count": 10,
        "slots": [{"time": f"{i}:00 PM", "source": 1} for i in range(5, 10)],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), 'button:has-text("Book")'
    )
    assert len(slots) == 5
    assert page.evaluate.await_count == 1, (
        f"Expected exactly one evaluate call; got {page.evaluate.await_count}"
    )


@pytest.mark.asyncio
async def test_collect_slots_multi_returns_slots_from_evaluate():
    """The wrapper builds AvailableSlot objects from the JS response."""
    checker = _make_checker()
    page = _make_page({
        "container_used": True,
        "button_count": 2,
        "slots": [
            {"time": "5:00 PM", "source": 1},
            {"time": "8:00 PM", "source": 2},
        ],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), 'button:has-text("Book")'
    )
    assert len(slots) == 2
    assert slots[0].slot_time == "5:00 PM"
    assert slots[1].slot_time == "8:00 PM"
    assert slots[0].slot_date == date(2026, 5, 15)
    assert slots[0].day_of_week == "Friday"


@pytest.mark.asyncio
async def test_collect_slots_multi_drops_null_times():
    """Apr 17 lesson: a slot with no extractable time is NOT emitted.
    The JS sets time=None when all 5 sources fail; wrapper drops it."""
    checker = _make_checker()
    page = _make_page({
        "container_used": True,
        "button_count": 3,
        "slots": [
            {"time": "5:00 PM", "source": 1},
            {"time": None, "source": -1},   # extraction failed
            {"time": "8:00 PM", "source": 2},
        ],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), 'button:has-text("Book")'
    )
    times = [s.slot_time for s in slots]
    assert times == ["5:00 PM", "8:00 PM"], (
        f"Null-time entry must be dropped, never fabricated as 'Slot N'; got {times}"
    )


@pytest.mark.asyncio
async def test_collect_slots_multi_uses_container_when_present():
    """When the JS reports container_used=True, no fallback warning is logged."""
    checker = _make_checker()
    page = _make_page({
        "container_used": True,
        "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1}],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), 'button:has-text("Book")'
    )
    assert len(slots) == 1
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    # The wrapper must have passed the container selector (so JS can scope)
    assert "containerSelector" in js_arg
    assert js_arg["containerSelector"]  # non-empty


@pytest.mark.asyncio
async def test_collect_slots_multi_falls_back_when_container_missing(caplog):
    """When the JS reports container_used=False (slots_container not in DOM),
    the wrapper logs a fallback debug message and still returns the slots."""
    import logging as _logging

    checker = _make_checker()
    page = _make_page({
        "container_used": False,
        "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1}],
    })

    with caplog.at_level(_logging.DEBUG):
        slots = await checker._collect_slots_multi(
            page, date(2026, 5, 15), 'button:has-text("Book")'
        )
    assert len(slots) == 1
    assert any(
        "slots_container not found" in rec.message
        and "page-wide" in rec.message
        for rec in caplog.records
    ), f"Expected a fallback debug log; got: {[r.message for r in caplog.records]}"


@pytest.mark.asyncio
async def test_collect_slots_multi_handles_evaluate_failure_gracefully():
    """If page.evaluate raises (CF challenge, page closed, etc.), the
    wrapper logs and returns []."""
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=Exception("page closed"))

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), 'button:has-text("Book")'
    )
    assert slots == []


@pytest.mark.asyncio
async def test_collect_slots_multi_passes_correct_args_to_js():
    """Wrapper sends the matched selector, container selector, slot-time-text
    selector, and time pattern to the JS in one call."""
    from src.selectors import SELECTORS

    checker = _make_checker()
    page = _make_page({
        "container_used": True,
        "button_count": 0,
        "slots": [],
    })

    matched = "button.Consumer-resultsListItem.is-available"
    await checker._collect_slots_multi(page, date(2026, 5, 15), matched)
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    assert js_arg["matchedSelector"] == matched
    assert js_arg["containerSelector"] == SELECTORS["slots_container"]
    assert js_arg["slotTimeTextSelector"] == SELECTORS["slot_time_text"]
    assert "timeRegex" in js_arg
