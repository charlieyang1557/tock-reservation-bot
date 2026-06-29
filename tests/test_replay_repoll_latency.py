"""Unit 3 (06/26 incident P0a) — rapid replay RE-POLL before the DOM safety net.

06/26 timeline: at the 20:00:00 window the post-release replay returned empty
(the release had not flipped to bookable yet), so _replay_first_detect ran the
full ~7s DOM safety-net scan SERIALLY before the next poll could re-check replay.
The release became bookable during those 7s but wasn't acted on until the next
poll at 20:00:07 — first Book click at 20:00:11 (~11s after the drop).

Fix: in sniper mode, an empty replay triggers a short, rapid replay re-poll loop
(catches a mid-poll flip in ~160ms) BEFORE paying the multi-second DOM scan. The
DOM safety net is preserved — it still runs if replay stays empty.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_checker():
    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="fui-hui-hua",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=True,
    )
    return AvailabilityChecker(cfg, MagicMock(), MagicMock())


def _slot():
    target = date.today() + timedelta(days=14)
    return AvailableSlot(slot_date=target, slot_time="5:00 PM",
                         day_of_week=target.strftime("%A"))


@pytest.mark.asyncio
async def test_repolls_replay_before_dom_scan_when_release_flips(monkeypatch):
    """Empty first replay → re-poll catches the flip → NO DOM scan."""
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)
    checker = _make_checker()
    found = [_slot()]

    calls = {"n": 0}
    async def fake_replay(*a, **k):
        calls["n"] += 1
        return [] if calls["n"] == 1 else found  # empty, then bookable
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_record_slots", lambda *a, **k: None)

    dom_called = []
    async def fake_dom_scan():
        dom_called.append(True)
        return []

    result = await checker._replay_first_detect(fake_dom_scan, keep_pages=True)
    assert result == found
    assert calls["n"] >= 2, "must re-poll replay after the first empty result"
    assert dom_called == [], "must NOT run the DOM safety-net scan once re-poll found slots"


@pytest.mark.asyncio
async def test_falls_back_to_dom_when_repoll_stays_empty(monkeypatch):
    """Replay empty across all re-polls → DOM safety net still runs (preserved)."""
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_MAX_TRIES", 3)
    checker = _make_checker()

    async def fake_replay(*a, **k):
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_log_replay_diag", lambda **k: None)
    monkeypatch.setattr(checker, "_capture_replay_miss", lambda *a, **k: None)

    dom_called = []
    async def fake_dom_scan():
        dom_called.append(True)
        return []

    result = await checker._replay_first_detect(fake_dom_scan, keep_pages=True)
    assert dom_called == [True], "DOM safety net must run when replay stays empty"
    assert result == []


@pytest.mark.asyncio
async def test_no_repoll_when_not_sniper(monkeypatch):
    """Outside sniper mode (keep_pages=False) replay is tried ONCE, then DOM —
    no re-poll latency added to ordinary polls."""
    checker = _make_checker()
    calls = {"n": 0}
    async def fake_replay(*a, **k):
        calls["n"] += 1
        return []
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_log_replay_diag", lambda **k: None)
    monkeypatch.setattr(checker, "_capture_replay_miss", lambda *a, **k: None)

    async def fake_dom_scan():
        return []

    await checker._replay_first_detect(fake_dom_scan, keep_pages=False)
    assert calls["n"] == 1, "non-sniper mode must not re-poll replay"


@pytest.mark.asyncio
async def test_repoll_failures_collapse_to_one_for_circuit_breaker(monkeypatch):
    """Re-poll FAILURES within a single detection must count as ONE logical
    failure. Otherwise N rapid re-polls would trip the 3-strike replay circuit
    breaker within one poll and disable replay for the whole release window."""
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_MAX_TRIES", 6)
    checker = _make_checker()

    async def fake_replay(*a, **k):
        # Mimic _try_calendar_replay's per-failure increment on a fetch failure.
        checker._replay_failure_count += 1
        return None
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_log_replay_diag", lambda **k: None)
    monkeypatch.setattr(checker, "_capture_replay_miss", lambda *a, **k: None)

    async def fake_dom_scan():
        return []

    await checker._replay_first_detect(fake_dom_scan, keep_pages=True)
    assert checker._replay_failure_count == 1, (
        f"re-poll failures must collapse to 1, got {checker._replay_failure_count}"
    )
    assert checker._replay_circuit_open is False, "breaker must NOT open from one poll's re-polls"


@pytest.mark.asyncio
async def test_first_poll_hit_returns_immediately_no_repoll(monkeypatch):
    """If the very first replay finds slots, return immediately (existing fast
    path) — no re-poll, no DOM scan."""
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)
    checker = _make_checker()
    found = [_slot()]
    calls = {"n": 0}
    async def fake_replay(*a, **k):
        calls["n"] += 1
        return found
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_record_slots", lambda *a, **k: None)
    dom_called = []
    async def fake_dom_scan():
        dom_called.append(True)
        return []

    result = await checker._replay_first_detect(fake_dom_scan, keep_pages=True)
    assert result == found
    assert calls["n"] == 1, "first-poll hit must not trigger extra re-polls"
    assert dom_called == []
