"""Tests for Playwright-selector fallback in batched JS paths
(Codex review HIGH 1+2 fix).

The B1.2 / B1.3 batched JS calls pass `matched_selector` to
`document.querySelectorAll` inside `page.evaluate`. That breaks for
Playwright-only selectors like `'button:visible:has-text("Book")'` —
real Tock pages whose only available control is the generic Book
fallback would silently miss the click/collect.

Fix: detect PW selectors before calling JS and fall back to a
locator-based Python iteration in those cases.
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailableSlot


# ---------------------------------------------------------------------------
# Helper detection
# ---------------------------------------------------------------------------

def test_helper_detects_playwright_specific_selectors():
    """The `:has-text`, `:text(`, `:visible` patterns make a selector
    Playwright-only and unsafe for document.querySelectorAll."""
    from src.selectors import is_playwright_selector

    assert is_playwright_selector('button:visible:has-text("Book")') is True
    assert is_playwright_selector('button:text("Book now")') is True
    assert is_playwright_selector('a:text("Book now")') is True
    assert is_playwright_selector('div:visible') is True
    # mixed selector containing any pw piece is pw
    assert is_playwright_selector(
        'button:text("Book now"), [data-testid="book-now"]'
    ) is True


def test_helper_recognizes_plain_css_selectors():
    """Pure-CSS selectors are safe for document.querySelectorAll."""
    from src.selectors import is_playwright_selector

    assert is_playwright_selector("button.Consumer-resultsListItem.is-available") is False
    assert is_playwright_selector("[data-testid='book-button']") is False
    assert is_playwright_selector(
        'button.Consumer-resultsListItem'
    ) is False


# ---------------------------------------------------------------------------
# _click_time_slot: PW selector must NOT call evaluate
# ---------------------------------------------------------------------------

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


@pytest.mark.asyncio
async def test_click_time_slot_pw_selector_falls_back_to_locator_iteration():
    """When the matched selector is PW-only, the booker must NOT call
    page.evaluate (it would throw DOM SyntaxError) — instead, it iterates
    via page.locator(...).nth() and clicks the matching button."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=True)
    # CRITICAL: page.evaluate must NOT be invoked for PW selectors
    page.evaluate = AsyncMock(side_effect=AssertionError(
        "evaluate must not be called for Playwright-only selectors — "
        "they break document.querySelectorAll"
    ))

    # First two PW-style selectors return 0; book-now returns 1
    pw_target = 'button:visible:has-text("Book")'
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value="5:00 PM\nBook")
    btn.click = AsyncMock()

    def make_locator(selector):
        loc = MagicMock()
        if selector == pw_target:
            loc.count = AsyncMock(return_value=1)
            loc.nth = MagicMock(return_value=btn)
        else:
            loc.count = AsyncMock(return_value=0)
        return loc
    page.locator = MagicMock(side_effect=make_locator)

    result = await booker._click_time_slot(page, slot)
    assert result is True
    btn.click.assert_awaited_once()


# ---------------------------------------------------------------------------
# _collect_slots_multi: PW selector must NOT call evaluate
# ---------------------------------------------------------------------------

def _make_checker():
    from src.checker import AvailabilityChecker
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
    return AvailabilityChecker(config, MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_collect_slots_multi_pw_selector_falls_back_to_locator_iteration():
    """When the matched selector is PW-only, the checker must NOT call
    page.evaluate — instead, it iterates via page.locator(...).nth() and
    extracts time per source, just like the old impl."""
    checker = _make_checker()

    page = AsyncMock()
    # CRITICAL: evaluate must not be the data path for PW selectors
    page.evaluate = AsyncMock(side_effect=AssertionError(
        "evaluate must not be called for Playwright-only selectors — "
        "they break document.querySelectorAll"
    ))

    # Slot button whose parent text contains the time
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value="Book")
    btn.get_attribute = AsyncMock(return_value=None)

    parent = AsyncMock()
    parent.text_content = AsyncMock(return_value="5:00 PM table for 2")
    no_time_anc = AsyncMock()
    no_time_anc.text_content = AsyncMock(return_value="")
    no_time_anc.locator = MagicMock(return_value=no_time_anc)
    parent.locator = MagicMock(return_value=no_time_anc)

    # Empty time-span (source 1 misses)
    empty_span_finder = MagicMock()
    empty_span_finder.count = AsyncMock(return_value=0)

    btn.locator = MagicMock(side_effect=lambda s: (
        parent if s == ".." else empty_span_finder
    ))

    pw_target = 'button:visible:has-text("Book")'
    button_locator = MagicMock()
    button_locator.count = AsyncMock(return_value=1)
    button_locator.nth = MagicMock(return_value=btn)

    # The container check should also work on the locator path; return 0
    # so we exercise the page-wide fallback.
    container_locator = MagicMock()
    container_locator.count = AsyncMock(return_value=0)

    def page_locator(selector):
        from src.selectors import SELECTORS
        if selector == SELECTORS["slots_container"]:
            return container_locator
        if selector == pw_target:
            return button_locator
        zero = MagicMock()
        zero.count = AsyncMock(return_value=0)
        return zero
    page.locator = MagicMock(side_effect=page_locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15), pw_target
    )

    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_collect_slots_multi_css_selector_still_uses_evaluate():
    """For CSS selectors (the common case), the JS fast path is preserved."""
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True,
        "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1}],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 5, 15),
        "button.Consumer-resultsListItem.is-available",
    )

    assert len(slots) == 1
    page.evaluate.assert_awaited_once()


@pytest.mark.asyncio
async def test_click_time_slot_css_selector_still_uses_evaluate():
    """CSS-only selector → JS fast path preserved."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")
    page = AsyncMock()
    page.wait_for_selector = AsyncMock(return_value=True)
    page.evaluate = AsyncMock(return_value={
        "clicked": True, "text": "5:00 PM", "reason": "exact",
    })

    css_target = "button.Consumer-resultsListItem.is-available"
    locator_mock = MagicMock()
    locator_mock.count = AsyncMock(return_value=1)
    page.locator = MagicMock(return_value=locator_mock)

    result = await booker._click_time_slot(page, slot)
    assert result is True
    page.evaluate.assert_awaited_once()
