"""Tests for the AvailabilityChecker.check_all integration with the
calendar-replay fast-path (USE_CALENDAR_REPLAY=true)."""
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_config(use_replay: bool = False) -> Config:
    return Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=use_replay,
    )


def _make_checker(use_replay: bool = False):
    cfg = _make_config(use_replay=use_replay)
    browser = MagicMock()
    tracker = MagicMock()
    return AvailabilityChecker(cfg, browser, tracker)


# ---------------------------------------------------------------------------
# Flag OFF: existing path runs unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_off_does_not_invoke_replay(monkeypatch):
    """With USE_CALENDAR_REPLAY=false, _try_calendar_replay must NOT be
    called — the existing per-date path runs as before."""
    checker = _make_checker(use_replay=False)
    replay_called = []

    async def fake_try_replay(*args, **kwargs):
        replay_called.append(True)
        return None
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_try_replay)
    # Stub out the legacy scan path
    monkeypatch.setattr(checker, "_get_target_dates", lambda *a, **k: [])

    await checker.check_all(concurrent=True)
    assert replay_called == [], (
        "Replay path must not be invoked when USE_CALENDAR_REPLAY=false"
    )


# ---------------------------------------------------------------------------
# Flag ON: success path returns slots from replay, skips per-date scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_replay_success_returns_slots(monkeypatch):
    """When replay returns slots, check_all returns them and does NOT
    run the per-date scan."""
    checker = _make_checker(use_replay=True)
    target_date = date.today() + timedelta(days=14)
    expected_slots = [
        AvailableSlot(slot_date=target_date, slot_time="5:00 PM",
                      day_of_week=target_date.strftime("%A")),
    ]

    async def fake_try_replay(*args, **kwargs):
        return expected_slots
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_try_replay)

    scan_called = []
    async def fake_check_date(*args, **kwargs):
        scan_called.append(True)
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    result = await checker.check_all(concurrent=True)
    assert result == expected_slots
    assert scan_called == [], (
        "Per-date scan must not run when replay succeeded"
    )
    # tracker.record should have fired for each slot
    assert checker.tracker.record.call_count == len(expected_slots)


# ---------------------------------------------------------------------------
# Flag ON: replay returns None → fall through to per-date scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_flag_on_replay_none_falls_through_to_scan(monkeypatch):
    """When replay returns None (init failed / fetch failed / parse
    failed), check_all must fall through to the legacy per-date scan."""
    checker = _make_checker(use_replay=True)

    async def fake_try_replay(*args, **kwargs):
        return None
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_try_replay)

    scan_called = []
    async def fake_check_date(d, **kwargs):
        scan_called.append(d)
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)

    await checker.check_all(concurrent=True)
    # Per-date scan should have fired
    assert len(scan_called) > 0, (
        "Legacy per-date scan must run when replay returns None"
    )


# ---------------------------------------------------------------------------
# _try_calendar_replay internals
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_try_replay_initializes_session_lazily(monkeypatch):
    """First call to _try_calendar_replay initializes a session via
    initialize_replay_session; subsequent calls reuse it."""
    checker = _make_checker(use_replay=True)
    init_calls = []
    fake_session = MagicMock()
    fake_session.consecutive_failures = 0

    async def fake_init(*args, **kwargs):
        init_calls.append(args)
        return fake_session
    async def fake_fetch(s):
        return b"\x0a\x0a2026-05-15\x1a\x0517:30"
    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", fake_init
    )
    monkeypatch.setattr(
        "src.calendar_replay.fetch_calendar", fake_fetch
    )

    # First call: init
    target = date.today() + timedelta(days=14)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [target] if "Friday" in days else [],
    )
    await checker._try_calendar_replay(sniper_mode=False)
    assert len(init_calls) == 1

    # Second call: should reuse the session, no re-init
    await checker._try_calendar_replay(sniper_mode=False)
    assert len(init_calls) == 1, "session must be reused after first init"


@pytest.mark.asyncio
async def test_try_replay_reinitializes_after_failure(monkeypatch):
    """If consecutive_failures > 0 on the cached session, the next call
    closes it and re-initializes."""
    checker = _make_checker(use_replay=True)
    init_calls = []
    close_calls = []
    fake_session_a = MagicMock(consecutive_failures=2)
    fake_session_b = MagicMock(consecutive_failures=0)
    # init only ever returns the FRESH session — the stale one
    # was pre-seeded directly into checker._replay_session below.

    async def fake_init(*args, **kwargs):
        init_calls.append(args)
        return fake_session_b
    async def fake_fetch(s):
        return b"\x0a\x0a2026-05-15\x1a\x0517:30"
    async def fake_close(s):
        close_calls.append(s)
    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", fake_init
    )
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", fake_fetch)
    monkeypatch.setattr("src.calendar_replay.close_session", fake_close)

    # Pre-seed a stale session
    checker._replay_session = fake_session_a

    target = date.today() + timedelta(days=14)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [target] if "Friday" in days else [],
    )
    await checker._try_calendar_replay(sniper_mode=False)

    assert close_calls == [fake_session_a], (
        "Stale session with consecutive_failures must be closed"
    )
    assert len(init_calls) == 1, (
        "A fresh session must be initialized after closing the stale one"
    )
    assert checker._replay_session is fake_session_b


@pytest.mark.asyncio
async def test_try_replay_returns_none_when_init_fails(monkeypatch):
    """If initialize_replay_session returns None (CF challenge, etc.),
    _try_calendar_replay returns None and does not raise."""
    checker = _make_checker(use_replay=True)
    async def fake_init(*args, **kwargs):
        return None
    async def fake_fetch(s):
        return b""
    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", fake_init
    )
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", fake_fetch)

    target = date.today() + timedelta(days=14)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [target] if "Friday" in days else [],
    )
    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result is None


@pytest.mark.asyncio
async def test_try_replay_returns_none_when_fetch_fails(monkeypatch):
    """If fetch_calendar returns None (auth expired, network error),
    _try_calendar_replay returns None."""
    checker = _make_checker(use_replay=True)
    fake_session = MagicMock(consecutive_failures=0)
    async def fake_init(*args, **kwargs):
        return fake_session
    async def fake_fetch(s):
        return None
    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", fake_init
    )
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", fake_fetch)

    target = date.today() + timedelta(days=14)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [target] if "Friday" in days else [],
    )
    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result is None


@pytest.mark.asyncio
async def test_close_replay_session_clears_state(monkeypatch):
    """close_replay_session() releases the cached session so the next
    call to _try_calendar_replay re-initializes."""
    checker = _make_checker(use_replay=True)
    fake_session = MagicMock()
    closed = []
    async def fake_close(s):
        closed.append(s)
    monkeypatch.setattr("src.calendar_replay.close_session", fake_close)

    checker._replay_session = fake_session
    await checker.close_replay_session()
    assert checker._replay_session is None
    assert closed == [fake_session]
