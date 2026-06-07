"""Tests for the 2026-06-05 sniper post-mortem fixes (post-review hardened).

Fix 1 — Discord on booking failure (notifier.booking_failed + monitor wiring).
Fix 3 — click the exact slot the checker found:
    * tagging in BOTH the JS fast path AND the PW-locator fallback
    * CSS-safe, unique-per-button token  date#index  (no raw time → no escaping)
    * booker prefers the tagged click on a warm page, but on a MISS it falls
      back to a strict time-scan before giving up (a rerender can drop the tag
      while the slot is still bookable — Codex/code-review HIGH finding).
Fix 4 — self-diagnosing failures: dump the page DOM on click AND checkout
    failures, with 0700 perms and a file-count cap.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot
from src.notifier import _RED


# ---------------------------------------------------------------------------
# Shared builders
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    from src.config import Config
    kwargs = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_notifier(**cfg_overrides):
    from src.notifier import Notifier
    return Notifier(_make_config(**cfg_overrides))


def _make_booker(**cfg_overrides):
    from src.booker import TockBooker
    return TockBooker(_make_config(**cfg_overrides), MagicMock(), MagicMock())


def _make_checker(**cfg_overrides):
    from src.checker import AvailabilityChecker
    return AvailabilityChecker(_make_config(**cfg_overrides), MagicMock(), MagicMock())


def _slot(d=date(2026, 5, 15), t="5:00 PM", dow="Friday"):
    return AvailableSlot(slot_date=d, slot_time=t, day_of_week=dow)


# ---------------------------------------------------------------------------
# Fix 1a — notifier.booking_failed: critical RED embed summarizing slots
# ---------------------------------------------------------------------------

def test_booking_failed_fires_critical_red_embed_for_all_slots():
    notifier = _make_notifier()
    notifier._fire = MagicMock()
    slots = [
        _slot(date(2026, 6, 12), "5:00 PM", "Friday"),
        _slot(date(2026, 6, 13), "5:00 PM", "Saturday"),
    ]

    notifier.booking_failed(slots, "no clickable slot button found")

    notifier._fire.assert_called_once()
    kwargs = notifier._fire.call_args.kwargs
    assert "Failed" in kwargs.get("title", "")
    assert "2 slot" in kwargs.get("title", "") or "2 slot" in kwargs.get("description", "")
    desc = kwargs.get("description", "")
    assert "2026-06-12" in desc and "2026-06-13" in desc
    assert "no clickable slot button found" in desc
    assert kwargs.get("color") == _RED
    assert kwargs.get("critical") is True


def test_booking_failed_accepts_single_slot():
    notifier = _make_notifier()
    notifier._fire = MagicMock()
    notifier.booking_failed(_slot(), "reason")
    assert "1 slot" in notifier._fire.call_args.kwargs.get("title", "")


# ---------------------------------------------------------------------------
# Fix 1b — monitor FAILED branch wires the (formerly dead) notification
# ---------------------------------------------------------------------------

def _build_monitor(*, slots, sniper_active=False, dry_run=False):
    from src.monitor import TockMonitor
    cfg = _make_config(dry_run=dry_run)
    browser = MagicMock()
    browser.warm_session = AsyncMock()

    checker = MagicMock()
    checker.check_all = AsyncMock(return_value=slots)
    checker.last_checks = max(1, len(slots))
    checker.last_errors = 0
    checker.close_sniper_pages = AsyncMock()
    checker.close_replay_session = AsyncMock()
    checker.close_handoff_pages = AsyncMock()
    checker.flush_deferred = MagicMock()
    checker.pop_handoff_page = MagicMock(return_value=None)
    checker.pop_warm_page = MagicMock(return_value=None)

    notifier = MagicMock()
    tracker = MagicMock()
    tracker.flush_deferred = MagicMock()

    with patch("src.monitor.TockBooker"):
        monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    monitor._sniper_active = sniper_active
    monitor._sniper_concurrent = True
    monitor._booking_secured = False
    return monitor, checker, notifier


@pytest.mark.asyncio
async def test_poll_failed_booking_notifies_booking_failed():
    from src.booker import BookingOutcome
    slots = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    monitor, _checker, notifier = _build_monitor(slots=slots)
    monitor.booker.book_best_slot_race = AsyncMock(
        return_value=(BookingOutcome.FAILED, None)
    )

    await monitor.poll()

    notifier.booking_failed.assert_called_once()
    assert list(notifier.booking_failed.call_args.args[0]) == slots


@pytest.mark.asyncio
async def test_poll_confirmed_booking_does_not_notify_failure():
    from src.booker import BookingOutcome
    slots = [_slot(date(2026, 6, 13), "5:00 PM", "Saturday")]
    monitor, _checker, notifier = _build_monitor(slots=slots)
    monitor.booker.book_best_slot_race = AsyncMock(
        return_value=(BookingOutcome.CONFIRMED, slots[0])
    )

    await monitor.poll()

    notifier.booking_failed.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 3a — AvailableSlot.target_selector
# ---------------------------------------------------------------------------

def test_available_slot_target_selector_defaults_to_none():
    assert _slot().target_selector is None


def test_available_slot_target_selector_excluded_from_equality():
    a = _slot()
    b = _slot()
    b.target_selector = '[data-sniper-target="2026-05-15#0"]'
    assert a == b


# ---------------------------------------------------------------------------
# Fix 3b — checker tags found buttons with a unique CSS-safe token
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pw_collect_tags_button_with_indexed_token():
    """PW-locator path tags the button date#index (CSS-safe, no raw time)."""
    checker = _make_checker()
    pw_target = 'button:visible:has-text("Book")'

    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value="Book")
    btn.get_attribute = AsyncMock(return_value=None)
    btn.evaluate = AsyncMock()
    parent = AsyncMock()
    parent.text_content = AsyncMock(return_value="5:00 PM table for 2")
    empty_span = MagicMock(); empty_span.count = AsyncMock(return_value=0)
    btn.locator = MagicMock(side_effect=lambda s: parent if s == ".." else empty_span)

    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=AssertionError("PW path must not evaluate"))
    button_locator = MagicMock()
    button_locator.count = AsyncMock(return_value=1)
    button_locator.nth = MagicMock(return_value=btn)
    container_locator = MagicMock(); container_locator.count = AsyncMock(return_value=0)

    def page_locator(selector):
        from src.selectors import SELECTORS
        if selector == SELECTORS["slots_container"]:
            return container_locator
        if selector == pw_target:
            return button_locator
        z = MagicMock(); z.count = AsyncMock(return_value=0)
        return z
    page.locator = MagicMock(side_effect=page_locator)

    slots = await checker._collect_slots_multi(page, date(2026, 6, 13), pw_target)

    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"
    assert slots[0].target_selector == '[data-sniper-target="2026-06-13#0"]'
    btn.evaluate.assert_awaited_once()
    assert btn.evaluate.call_args.args[1] == "2026-06-13#0"


@pytest.mark.asyncio
async def test_pw_collect_two_same_time_buttons_get_distinct_tokens():
    """Duplicate same-date+time buttons must get distinct selectors so the
    booker never clicks the wrong one via .first (Codex MEDIUM)."""
    checker = _make_checker()
    pw_target = 'button:visible:has-text("Book")'

    def make_btn():
        b = AsyncMock()
        b.text_content = AsyncMock(return_value="Book")
        b.get_attribute = AsyncMock(return_value=None)
        b.evaluate = AsyncMock()
        par = AsyncMock(); par.text_content = AsyncMock(return_value="5:00 PM")
        es = MagicMock(); es.count = AsyncMock(return_value=0)
        b.locator = MagicMock(side_effect=lambda s, par=par, es=es: par if s == ".." else es)
        return b
    b0, b1 = make_btn(), make_btn()

    page = AsyncMock()
    button_locator = MagicMock()
    button_locator.count = AsyncMock(return_value=2)
    button_locator.nth = MagicMock(side_effect=lambda i: (b0, b1)[i])
    container_locator = MagicMock(); container_locator.count = AsyncMock(return_value=0)

    def page_locator(selector):
        from src.selectors import SELECTORS
        if selector == SELECTORS["slots_container"]:
            return container_locator
        if selector == pw_target:
            return button_locator
        z = MagicMock(); z.count = AsyncMock(return_value=0)
        return z
    page.locator = MagicMock(side_effect=page_locator)

    slots = await checker._collect_slots_multi(page, date(2026, 6, 13), pw_target)

    sels = {s.target_selector for s in slots}
    assert sels == {
        '[data-sniper-target="2026-06-13#0"]',
        '[data-sniper-target="2026-06-13#1"]',
    }


@pytest.mark.asyncio
async def test_js_collect_sets_target_selector_from_token():
    """The JS fast path (CSS selectors — the COMMON release-window path) must
    also tag and propagate target_selector. Regression for code-review CRITICAL."""
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True, "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1, "target": "2026-06-13#0"}],
    })

    slots = await checker._collect_slots_multi(
        page, date(2026, 6, 13), "button.Consumer-resultsListItem.is-available"
    )

    assert len(slots) == 1
    assert slots[0].target_selector == '[data-sniper-target="2026-06-13#0"]'
    # dateStr must be handed to the JS so it can build the token.
    assert page.evaluate.call_args.args[1].get("dateStr") == "2026-06-13"


@pytest.mark.asyncio
async def test_js_collect_without_token_leaves_target_selector_none():
    checker = _make_checker()
    page = AsyncMock()
    page.evaluate = AsyncMock(return_value={
        "container_used": True, "button_count": 1,
        "slots": [{"time": "5:00 PM", "source": 1}],  # no 'target'
    })
    slots = await checker._collect_slots_multi(
        page, date(2026, 6, 13), "button.Consumer-resultsListItem.is-available"
    )
    assert slots[0].target_selector is None


# ---------------------------------------------------------------------------
# Fix 3c — booker._click_tagged_slot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_click_tagged_slot_clicks_when_tag_present():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    btn = AsyncMock(); btn.click = AsyncMock()
    loc = MagicMock(); loc.count = AsyncMock(return_value=1); loc.first = btn
    page = AsyncMock(); page.locator = MagicMock(return_value=loc)

    assert await booker._click_tagged_slot(page, slot) is True
    btn.click.assert_awaited_once()
    page.locator.assert_called_once_with('[data-sniper-target="2026-05-15#0"]')


@pytest.mark.asyncio
async def test_click_tagged_slot_passes_bounded_timeout():
    """The click must not inherit Playwright's 30s default on the hot path."""
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    btn = AsyncMock(); btn.click = AsyncMock()
    loc = MagicMock(); loc.count = AsyncMock(return_value=1); loc.first = btn
    page = AsyncMock(); page.locator = MagicMock(return_value=loc)

    await booker._click_tagged_slot(page, slot)
    timeout = btn.click.call_args.kwargs.get("timeout")
    assert timeout is not None and 0 < timeout <= 5000


@pytest.mark.asyncio
async def test_click_tagged_slot_false_when_tag_absent():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    loc = MagicMock(); loc.count = AsyncMock(return_value=0)
    page = AsyncMock(); page.locator = MagicMock(return_value=loc)
    assert await booker._click_tagged_slot(page, slot) is False


@pytest.mark.asyncio
async def test_click_tagged_slot_false_on_exception():
    booker = _make_booker()
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    page = AsyncMock()
    page.locator = MagicMock(side_effect=RuntimeError("detached"))
    assert await booker._click_tagged_slot(page, slot) is False


@pytest.mark.asyncio
async def test_click_tagged_slot_false_without_target_selector():
    booker = _make_booker()
    slot = _slot()
    page = AsyncMock(); page.locator = MagicMock()
    assert await booker._click_tagged_slot(page, slot) is False
    page.locator.assert_not_called()


# ---------------------------------------------------------------------------
# Fix 3d — _book_single warm-page routing + tag-miss fallback (HIGH)
# ---------------------------------------------------------------------------

def _warm_page():
    p = AsyncMock()
    p.is_closed = MagicMock(return_value=False)
    p.close = AsyncMock()
    return p


@pytest.mark.asyncio
async def test_book_single_warm_tag_hit_skips_time_scan():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=True)
    booker._click_time_slot = AsyncMock(return_value=True)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)  # end after click stage

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())

    assert ok is False  # checkout returned False
    booker._click_tagged_slot.assert_awaited_once()
    booker._click_time_slot.assert_not_awaited()
    booker._wait_for_checkout.assert_awaited_once()  # reached checkout → click succeeded


@pytest.mark.asyncio
async def test_book_single_warm_tag_miss_falls_back_to_strict_scan_then_dumps():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=False)  # tag gone
    booker._click_time_slot = AsyncMock(return_value=False)    # rescan also fails
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())

    assert ok is False
    booker._click_tagged_slot.assert_awaited_once()
    booker._click_time_slot.assert_awaited_once()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is True
    booker._dump_click_failure.assert_awaited_once()
    booker._wait_for_checkout.assert_not_awaited()  # never passed the click stage


@pytest.mark.asyncio
async def test_book_single_warm_tag_miss_fallback_succeeds_proceeds():
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._click_time_slot = AsyncMock(return_value=True)  # rescan recovers the slot
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)

    await booker._book_single(slot, asyncio.Event(), warm_page=_warm_page())

    booker._click_time_slot.assert_awaited_once()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is True
    booker._wait_for_checkout.assert_awaited_once()  # fallback recovered → proceeded


@pytest.mark.asyncio
async def test_book_single_fresh_page_ignores_target_selector():
    """A fresh-nav page has no tag even if the slot carries target_selector;
    it must use the normal (non-strict) time-scan, never the tagged click."""
    booker = _make_booker(dry_run=False)
    slot = _slot(); slot.target_selector = '[data-sniper-target="2026-05-15#0"]'

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.close = AsyncMock()
    booker.browser.new_page = AsyncMock(return_value=page)
    booker.browser.page_pool = None
    booker._wait_for_selector = AsyncMock(return_value=True)
    booker._click_calendar_day = AsyncMock(return_value=True)
    booker._click_tagged_slot = AsyncMock(return_value=True)   # must NOT be used
    booker._click_time_slot = AsyncMock(return_value=False)    # end quickly
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=None)

    assert ok is False
    booker._click_tagged_slot.assert_not_awaited()
    booker._click_time_slot.assert_awaited()
    assert booker._click_time_slot.call_args.kwargs.get("strict_time_match") is False


# ---------------------------------------------------------------------------
# Fix 4 — DOM dump on failure (perms + cap + content)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dump_click_failure_writes_page_html(tmp_path, monkeypatch):
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>SNIPER_DOM_MARKER</html>")
    page.url = "https://www.exploretock.com/test/search?date=2026-06-13&size=2"
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))

    await booker._dump_click_failure(page, _slot(date(2026, 6, 13), "5:00 PM", "Saturday"))

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    body = files[0].read_text()
    assert "SNIPER_DOM_MARKER" in body
    assert "2026-06-13" in files[0].name
    # URL query string (party size, date) must be stripped from the header.
    assert "size=2" not in body


@pytest.mark.asyncio
async def test_dump_click_failure_respects_file_cap(tmp_path, monkeypatch):
    booker = _make_booker()
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    monkeypatch.setattr("src.booker._MAX_FAILURE_DUMPS", 2)
    for i in range(2):
        (tmp_path / f"clickfail_2026010{i}_000000_000000_2026-06-13.html").write_text("x")

    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>OVERFLOW</html>")
    page.url = "https://x/search"
    await booker._dump_click_failure(page, _slot())

    # Cap respected: still only the 2 pre-existing files, no new dump.
    assert len(list(tmp_path.iterdir())) == 2


@pytest.mark.asyncio
async def test_dump_click_failure_swallows_content_errors(tmp_path, monkeypatch):
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(side_effect=RuntimeError("page closed"))
    monkeypatch.setattr("src.booker._BOOKING_FAILURE_DIR", str(tmp_path))
    await booker._dump_click_failure(page, _slot())
    assert list(tmp_path.iterdir()) == []
