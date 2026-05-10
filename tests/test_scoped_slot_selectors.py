"""Tests for container-scoped slot collection (A5 fix).

Post-B1.3: the container scoping happens inside one page.evaluate call.
The JS reports `container_used: bool` to indicate whether the container
was found and used. These tests now assert the wrapper passes the right
selector to JS and respects the container_used signal in its logging
(but the actual button-vs-non-button discrimination is enforced by the
JS scoping `root.querySelectorAll(matchedSelector)` inside the container
when present, document-wide otherwise).
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
    """When the JS reports container_used=True, the slots returned were
    scoped to the container (the JS does `root.querySelectorAll(matched)`
    where root = container element when present)."""
    checker = _make_checker()

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True,
        "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 2}],
    })

    import src.selectors as sel_mod
    monkeypatch.setitem(sel_mod.SELECTORS, "slots_container", "div.results-list")

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    # The wrapper passed both the matched selector and the container selector
    args, _ = page.evaluate.call_args
    js_arg = args[1] if len(args) > 1 else args[0]
    assert js_arg["containerSelector"] == "div.results-list"
    assert js_arg["matchedSelector"] == 'button:visible:has-text("Book")'


@pytest.mark.asyncio
async def test_falls_back_to_page_when_container_missing(monkeypatch):
    """When the JS reports container_used=False (the slots_container
    wasn't in the DOM), the wrapper logs the fallback and still returns
    the slots that were collected document-wide."""
    checker = _make_checker()

    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": False,
        "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 2}],
    })

    import src.selectors as sel_mod
    monkeypatch.setitem(sel_mod.SELECTORS, "slots_container", "div.results-list")

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "5:00 PM"
