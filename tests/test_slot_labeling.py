"""Tests for slot-label extraction priority (A3 fix).

The checker must extract a real time string in priority order:
  1. Child span (existing slot_time_text selector)
  2. Time pattern in parent.text_content()
  3. Time pattern in any ancestor up to 3 levels (NEW)
  4. Button's aria-label or title attribute (NEW)
  5. Button's own text content (if not bare "Book")

If none of the above yield a parseable time, the slot must NOT be
emitted. The "Slot N" fallback is removed.
"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker
from tests.conftest import make_page_locator


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


def _btn(*, text="", aria_label="", title="", parent_text="",
         grandparent_text="", great_grandparent_text="",
         time_span_text=None):
    """Build a mock locator behaving like a Tock slot button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.get_attribute = AsyncMock(side_effect=lambda name: {
        "aria-label": aria_label, "title": title,
    }.get(name, None))

    # Time-span child (level 0)
    time_span = AsyncMock()
    time_span.count = AsyncMock(return_value=1 if time_span_text else 0)
    span_first = AsyncMock()
    span_first.text_content = AsyncMock(return_value=time_span_text or "")
    time_span.first = span_first

    # Parent / grandparent / great-grandparent text
    ancestors = [parent_text, grandparent_text, great_grandparent_text]

    # Closure-state for ancestor-walk depth (list-cell idiom — avoids
    # assigning attributes to a function, which type checkers flag)
    depth_state = [0]

    def locator_factory(selector: str):
        if selector == "..":
            depth_state[0] += 1
            depth = depth_state[0]
            anc = AsyncMock()
            anc.text_content = AsyncMock(
                return_value=ancestors[depth - 1] if depth <= 3 else ""
            )
            anc.locator = MagicMock(side_effect=locator_factory)
            return anc
        # time_span selector — return the prepared time_span mock
        return time_span

    btn.locator = MagicMock(side_effect=locator_factory)
    return btn


@pytest.mark.asyncio
async def test_extracts_from_child_span():
    """Source 1: time in child span wins."""
    checker = _make_checker()
    btn = _btn(time_span_text="5:00 PM", text="ignored")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page = MagicMock()
    page.locator = make_page_locator(locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), "button.Consumer-resultsListItem.is-available"
    )
    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_extracts_from_aria_label():
    """Source 4: when nothing else has time, aria-label is consulted."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="Book table at 5:30 PM for 2 guests",
               parent_text="Book", grandparent_text="", great_grandparent_text="")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page = MagicMock()
    page.locator = make_page_locator(locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "5:30 PM"


@pytest.mark.asyncio
async def test_extracts_from_grandparent():
    """Source 3: time pattern in 2nd ancestor is found."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="", title="",
               parent_text="Book", grandparent_text="6:00 PM table for 2",
               great_grandparent_text="")
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page = MagicMock()
    page.locator = make_page_locator(locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "6:00 PM"


@pytest.mark.asyncio
async def test_no_time_anywhere_drops_slot():
    """If no time can be extracted from any source, the slot is NOT emitted.
    The 'Slot N' fallback is forbidden."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="", title="",
               parent_text="Book", grandparent_text="Restaurant info",
               great_grandparent_text="")
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page.locator = make_page_locator(locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert slots == [], f"Expected no slot when time cannot be extracted; got {slots}"


@pytest.mark.asyncio
async def test_no_slot_n_label_in_output():
    """Regression: 'Slot 1', 'Slot 2', etc. must never appear in slot_time."""
    checker = _make_checker()
    # 3 buttons, none with extractable time
    btns = [
        _btn(text="Book", parent_text="x", grandparent_text="y"),
        _btn(text="Book", parent_text="x", grandparent_text="y"),
        _btn(text="Book", parent_text="x", grandparent_text="y"),
    ]
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=3)
    locator.nth = MagicMock(side_effect=lambda i: btns[i])
    page.locator = make_page_locator(locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
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
