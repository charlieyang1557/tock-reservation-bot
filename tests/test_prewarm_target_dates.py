"""Tests for target-date page prewarm (Phase A+2 Task 2)."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

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
    browser = MagicMock()
    browser.new_page = AsyncMock()
    return AvailabilityChecker(config, browser, MagicMock())


@pytest.mark.asyncio
async def test_prewarm_opens_one_page_per_date():
    """prewarm_target_dates opens exactly N pages and stores them in _sniper_pages."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.goto = AsyncMock()
        p.wait_for_selector = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # Use stagger=0 to keep the test fast
    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert len(pages) == 3
    assert set(checker._sniper_pages.keys()) == {
        "2026-05-01", "2026-05-02", "2026-05-03"
    }


@pytest.mark.asyncio
async def test_prewarm_navigates_to_correct_url():
    """Each prewarmed page navigates to the per-date Tock search URL."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    page.goto.assert_called_once()
    args = page.goto.call_args
    url = args.args[0] if args.args else args.kwargs.get("url")
    assert "date=2026-05-01" in url
    assert "size=2" in url


@pytest.mark.asyncio
async def test_prewarm_waits_for_calendar_container():
    """After goto, prewarm waits for the calendar to render (parks at CALENDAR_LOADED)."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Confirm wait_for_selector was called with the calendar_container
    # selector — not just any selector. Tight assertion catches a future
    # refactor that swaps in a different (incorrect) wait target.
    import src.selectors as sel_mod
    page.wait_for_selector.assert_called_once()
    called_selector = page.wait_for_selector.call_args.args[0]
    assert called_selector == sel_mod.SELECTORS["calendar_container"], (
        f"Expected wait_for_selector to receive calendar_container "
        f"({sel_mod.SELECTORS['calendar_container']!r}); got {called_selector!r}"
    )


@pytest.mark.asyncio
async def test_prewarm_failure_does_not_break_other_dates():
    """If one prewarm fails, others still complete."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    page_count = [0]
    pages = []
    async def make_page():
        page_count[0] += 1
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.wait_for_selector = AsyncMock()
        if page_count[0] == 2:
            p.goto = AsyncMock(side_effect=Exception("fake CF error"))
        else:
            p.goto = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Two pages successfully prewarmed (1st and 3rd); 2nd failed but didn't kill the others
    assert len(checker._sniper_pages) == 2
    assert "2026-05-01" in checker._sniper_pages
    assert "2026-05-03" in checker._sniper_pages
    assert "2026-05-02" not in checker._sniper_pages


@pytest.mark.asyncio
async def test_prewarm_respects_stagger():
    """Pages open spread across `stagger_sec` intervals."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    sleep_calls = []
    real_sleep = asyncio.sleep
    async def fake_sleep(secs):
        sleep_calls.append(secs)
        # Don't actually sleep — keep the test fast
        await real_sleep(0)

    with patch("src.checker.asyncio.sleep", new=fake_sleep):
        await checker.prewarm_target_dates(dates, stagger_sec=30)

    # 3 dates → exactly 2 stagger sleeps (no trailing sleep after last date).
    # Tight assertion catches both off-by-one regressions and missing-stagger
    # regressions (a single `any()` would mask either of those).
    stagger_30_count = sum(1 for s in sleep_calls if s == 30)
    assert stagger_30_count == len(dates) - 1, (
        f"Expected exactly {len(dates) - 1} stagger sleeps of 30s; "
        f"got {stagger_30_count} (sleep_calls={sleep_calls})"
    )


@pytest.mark.asyncio
async def test_prewarm_all_dates_fail_returns_cleanly():
    """If EVERY date's prewarm fails, _sniper_pages stays empty and no
    exception propagates. Sniper-poll falls back to cold goto() for all."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.goto = AsyncMock(side_effect=Exception("simulated CF challenge"))
        p.wait_for_selector = AsyncMock()
        p.close = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # Must not raise even when every date fails
    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert checker._sniper_pages == {}, (
        f"All-fail must leave _sniper_pages empty; got {checker._sniper_pages}"
    )
    # All 3 leaked pages should have been closed by the new finally block (Fix 1)
    assert all(p.close.called for p in pages), (
        "Failed pages must be closed to prevent leak across release windows"
    )
