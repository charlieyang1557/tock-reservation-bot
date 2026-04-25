"""Tests for container-scoped slot collection (A5 fix).

When `slots_container` selector matches, slot lookups must be scoped to
that container. A `Book` button outside the container must NOT be collected.
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
    return AvailabilityChecker(config, MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_collect_only_buttons_inside_container(monkeypatch):
    """If slots_container is present, only buttons inside it are collected."""
    checker = _make_checker()

    # in_container: parent has time text → should be collected
    in_container_btn = AsyncMock()
    in_container_btn.text_content = AsyncMock(return_value="Book")
    in_container_btn.get_attribute = AsyncMock(return_value=None)

    parent_in = AsyncMock()
    parent_in.text_content = AsyncMock(return_value="5:00 PM   2 guests")
    parent_in.locator = MagicMock(return_value=AsyncMock(
        text_content=AsyncMock(return_value="")
    ))

    in_container_btn.locator = MagicMock(side_effect=lambda s: (
        parent_in if s == ".." else AsyncMock(count=AsyncMock(return_value=0))
    ))

    # Container locator (page-level): find -> exists, then locator(button…) returns 1
    container = MagicMock()
    container_buttons = MagicMock()
    container_buttons.count = AsyncMock(return_value=1)
    container_buttons.nth = MagicMock(return_value=in_container_btn)
    container.locator = MagicMock(return_value=container_buttons)

    page = MagicMock()
    container_finder = MagicMock()
    container_finder.count = AsyncMock(return_value=1)
    container_finder.first = container

    def page_locator(sel):
        if "slots_container" in sel or "results-list" in sel:
            return container_finder
        # Anything else returning 1 button means the test misconfigured scoping
        page_wide = MagicMock()
        page_wide.count = AsyncMock(return_value=99)  # noisy false positive
        return page_wide

    page.locator = MagicMock(side_effect=page_locator)

    # Pre-resolve the container-finder selector by registering it
    import src.selectors as sel_mod
    monkeypatch.setitem(sel_mod.SELECTORS, "slots_container", "div.results-list")

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    assert len(slots) == 1, (
        f"Expected exactly 1 slot from inside the container, got {len(slots)}: {slots}"
    )


@pytest.mark.asyncio
async def test_falls_back_to_page_when_container_missing(monkeypatch):
    """If slots_container is not present, falls back to page-wide collection."""
    checker = _make_checker()

    # Container selector returns 0 → falls back to page-level
    container_finder = MagicMock()
    container_finder.count = AsyncMock(return_value=0)

    page_wide_btn = AsyncMock()
    page_wide_btn.text_content = AsyncMock(return_value="Book")
    page_wide_btn.get_attribute = AsyncMock(return_value=None)
    page_wide_btn.locator = MagicMock(return_value=AsyncMock(
        text_content=AsyncMock(return_value="5:00 PM   table"),
        count=AsyncMock(return_value=0),
    ))

    page_wide_locator = MagicMock()
    page_wide_locator.count = AsyncMock(return_value=1)
    page_wide_locator.nth = MagicMock(return_value=page_wide_btn)

    page = MagicMock()
    def page_locator(sel):
        if "slots_container" in sel or "results-list" in sel:
            return container_finder
        return page_wide_locator
    page.locator = MagicMock(side_effect=page_locator)

    import src.selectors as sel_mod
    monkeypatch.setitem(sel_mod.SELECTORS, "slots_container", "div.results-list")

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    # Falls back to page-wide collection when container missing
    assert len(slots) == 1, (
        f"Expected fallback path to yield 1 slot from page-wide collection; got {slots}"
    )
    assert slots[0].slot_time.upper() == "5:00 PM"
