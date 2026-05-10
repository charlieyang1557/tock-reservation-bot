"""Tests for Codex B3 review fixes (1 HIGH + 3 MEDIUM).

HIGH 1: `_confirm_booking()` shim must NOT bypass the confirm lock
  Old (B3.1): the shim called prep + click+verify directly without
  acquiring `_confirm_lock` or checking `_confirm_attempted` —
  any concurrent caller could double-click confirm despite the new
  safety design.
  New: shim acquires the lock and respects `_confirm_attempted` so
  future callers can't accidentally bypass safety.

MEDIUM 1: payment-card wait must abort on booking_won
  Old: with no saved card, every racing slot's _prepare_for_confirm
  enters the 9-min reload loop independently — N*4 reloads/min.
  New: `_prepare_for_confirm` accepts an optional `booking_won`
  asyncio.Event; if set, the wait loop exits early.

MEDIUM 2: XHR telemetry flush must batch writes
  Old: `flush()` did `f.flush() + os.fsync()` PER LINE.
  New: write all lines first, then ONE flush + fsync.

MEDIUM 3: PagePool.acquire must validate page is still open
  Old: returned the popped page directly — could hand a closed
  page to the booker.
  New: pop-and-discard closed pages until we find one open OR
  fall through to new_page().
"""
import asyncio
from collections import deque
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# HIGH 1: _confirm_booking shim must acquire the lock + check the guard
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


@pytest.mark.asyncio
async def test_confirm_booking_shim_acquires_lock(monkeypatch):
    """Calling the shim must take `_confirm_lock` so concurrent shim
    invocations don't double-click confirm."""
    booker = _make_booker()
    page = AsyncMock()

    lock_held_during_call = []

    async def fake_click(p, s):
        lock_held_during_call.append(booker._confirm_lock.locked())
        return True

    async def fake_prep(p, s):
        return True

    monkeypatch.setattr(booker, "_prepare_for_confirm", fake_prep)
    monkeypatch.setattr(
        booker, "_execute_confirm_click_and_verify", fake_click
    )

    from src.checker import AvailableSlot
    slot = AvailableSlot(
        slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday"
    )
    await booker._confirm_booking(page, slot)
    assert lock_held_during_call == [True], (
        "_confirm_booking shim must hold _confirm_lock during the click — "
        "currently it bypasses the lock entirely (Codex HIGH 1)"
    )


@pytest.mark.asyncio
async def test_confirm_booking_shim_respects_confirm_attempted():
    """If `_confirm_attempted` is already set when the shim is called,
    it must NOT click again (returns False without calling
    _execute_confirm_click_and_verify)."""
    booker = _make_booker()
    booker._confirm_attempted.set()  # simulate prior soft-win

    page = AsyncMock()
    click_count = 0
    async def fake_click(p, s):
        nonlocal click_count
        click_count += 1
        return True
    booker._execute_confirm_click_and_verify = fake_click  # type: ignore[assignment]
    booker._prepare_for_confirm = AsyncMock(return_value=True)  # type: ignore[assignment]

    from src.checker import AvailableSlot
    slot = AvailableSlot(
        slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday"
    )
    result = await booker._confirm_booking(page, slot)
    assert result is False
    assert click_count == 0, (
        "_confirm_booking must respect _confirm_attempted — "
        "no click when prior unverified confirm exists"
    )


# ---------------------------------------------------------------------------
# MEDIUM 1: payment-card wait must abort on booking_won
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_prepare_for_confirm_accepts_booking_won_event():
    """Signature must allow callers to pass a booking_won event so the
    9-min payment-card wait exits early when another task wins."""
    import inspect
    booker = _make_booker()
    sig = inspect.signature(booker._prepare_for_confirm)
    assert "booking_won" in sig.parameters, (
        "_prepare_for_confirm(page, slot, booking_won=None) signature is "
        "required so racing tasks can short-circuit the payment-card wait"
    )


@pytest.mark.asyncio
async def test_payment_wait_loop_exits_when_booking_won():
    """When booking_won fires mid-wait, _prepare_for_confirm exits the
    9-min reload loop on the next iteration instead of reloading
    pointlessly."""
    booker = _make_booker()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/abc"

    # Simulate: needs payment, no card, wait loop runs
    booker._page_needs_payment = AsyncMock(return_value=True)  # type: ignore[assignment]
    booker._has_saved_card = AsyncMock(return_value=False)  # type: ignore[assignment]
    page.reload = AsyncMock()
    page.wait_for_timeout = AsyncMock()

    booking_won = asyncio.Event()

    # Set the event after first sleep so the loop exits on next iteration
    real_sleep = asyncio.sleep
    sleep_count = 0
    async def fake_sleep(secs):
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 1:
            booking_won.set()
        await real_sleep(0)

    import src.booker as booker_mod
    orig_sleep = booker_mod.asyncio.sleep
    booker_mod.asyncio.sleep = fake_sleep  # monkey-patch
    try:
        from src.checker import AvailableSlot
        slot = AvailableSlot(
            slot_date=date(2026, 5, 15), slot_time="5:00 PM", day_of_week="Friday"
        )
        result = await booker._prepare_for_confirm(page, slot, booking_won=booking_won)
    finally:
        booker_mod.asyncio.sleep = orig_sleep

    # Should have exited early (not stayed in the 9-min loop)
    assert result is False, (
        "When booking_won fires mid-wait, _prepare_for_confirm must "
        "abort and return False so the loser task doesn't keep reloading"
    )
    assert sleep_count <= 5, (
        f"Expected early abort (≤5 sleeps); got {sleep_count} — "
        "booking_won is being ignored"
    )


# ---------------------------------------------------------------------------
# MEDIUM 2: XHR telemetry flush must batch fsync
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_xhr_telemetry_flush_does_one_fsync_per_batch(tmp_path):
    """Old: fsync per line → 50–200 ms wall time per flush in sniper.
    New: write all lines, then ONE fsync at the end."""
    import os as os_mod
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="availability",
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    # Pre-load the buffer with 10 records
    for i in range(10):
        recorder._on_response(MagicMock(
            url=f"https://api.exploretock.com/availability?id={i}",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ))

    fsync_calls = 0
    real_fsync = os_mod.fsync
    def counted_fsync(fd):
        nonlocal fsync_calls
        fsync_calls += 1
        return real_fsync(fd)

    import src.xhr_telemetry as xhr_mod
    orig_fsync = xhr_mod.os.fsync
    xhr_mod.os.fsync = counted_fsync
    try:
        await recorder.flush()
    finally:
        xhr_mod.os.fsync = orig_fsync

    assert fsync_calls == 1, (
        f"Expected exactly ONE fsync per flush; got {fsync_calls}. "
        "Per-line fsync would block the event loop in sniper mode."
    )
    # Sanity: all 10 records made it to disk
    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 10


# ---------------------------------------------------------------------------
# MEDIUM 3: PagePool.acquire must validate page is still open
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_page_pool_acquire_skips_closed_pages():
    """If a pooled page is closed (crashed, manually-closed elsewhere),
    acquire() must NOT return it. Pop-and-discard until a usable page
    is found, or fall through to new_page()."""
    from src.page_pool import PagePool

    fresh_page = AsyncMock()
    fresh_page.is_closed = MagicMock(return_value=False)
    closed_page = AsyncMock()
    closed_page.is_closed = MagicMock(return_value=True)

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=fresh_page)

    pool = PagePool(browser, target_size=2)
    # Pre-populate: closed page first in the deque
    pool._pool.extend([closed_page, fresh_page])

    got = await pool.acquire()
    assert got is fresh_page, (
        "acquire() must skip the closed page and return the live one"
    )


@pytest.mark.asyncio
async def test_page_pool_acquire_falls_through_when_all_pooled_pages_closed():
    """If every pooled page is closed, acquire() must fall through to
    browser.new_page() — never return a closed page."""
    from src.page_pool import PagePool

    closed1 = AsyncMock()
    closed1.is_closed = MagicMock(return_value=True)
    closed2 = AsyncMock()
    closed2.is_closed = MagicMock(return_value=True)
    fresh_from_browser = AsyncMock()
    fresh_from_browser.is_closed = MagicMock(return_value=False)

    browser = MagicMock()
    browser.new_page = AsyncMock(return_value=fresh_from_browser)

    pool = PagePool(browser, target_size=2)
    pool._pool.extend([closed1, closed2])

    got = await pool.acquire()
    assert got is fresh_from_browser, (
        "All-closed pool must fall through to browser.new_page()"
    )
    assert browser.new_page.called
