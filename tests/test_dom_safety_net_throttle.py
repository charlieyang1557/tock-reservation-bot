"""06/26 incident P0 (Tier-1, part 2) — throttle the post-release DOM safety net.

06/26 forensics: on a clean-empty replay, _replay_first_detect ran the INLINE
serial ~7s DOM safety-net scan on EVERY empty poll, blocking the monitor's
strictly serial poll loop for 7s at the exact drop moment (and making every
post-race poll 8.8s).

Fix under test: post-release (age >= 60), on clean-empty replay the DOM
safety-net scan runs AT MOST once per _DOM_SAFETY_NET_MIN_INTERVAL_SEC (10s)
of wall clock (time.monotonic anchor):
  - FIRST post-release clean-empty poll: arm the anchor, do NOT scan (earliest
    possible scan is ~T+10, and only if replay is still empty by then — the
    replay false-negative case the safety net exists for).
  - anchor elapsed >= 10s: scan and re-anchor.
  - between scans: return [] immediately after the re-poll so the next replay
    poll runs at full cadence.
  - replay None (fetch/decode FAILURE) BYPASSES the throttle (replay is
    broken; DOM is the only detection left).
  - a successful replay detection still skips the DOM scan entirely.
Mocking style follows tests/test_replay_repoll_latency.py.
"""
import time
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


def _wire_empty(checker, monkeypatch, result):
    """Stub replay to always return *result*; silence diag/capture; return the
    dom_called spy list and the dom_scan coroutine."""
    async def fake_replay(*a, **k):
        return result
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)
    monkeypatch.setattr(checker, "_log_replay_diag", lambda **k: None)
    monkeypatch.setattr(checker, "_capture_replay_miss", lambda *a, **k: None)

    dom_called = []
    async def fake_dom_scan():
        dom_called.append(True)
        return []
    return dom_called, fake_dom_scan


@pytest.fixture(autouse=True)
def _fast_repoll(monkeypatch):
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_MAX_TRIES", 2)


@pytest.mark.asyncio
async def test_first_post_release_empty_poll_arms_anchor_without_scanning(monkeypatch):
    """First clean-empty poll of the window: initialize the anchor to now and
    do NOT scan — so the earliest possible DOM scan is ~T+10."""
    checker = _make_checker()
    assert checker._dom_safety_net_anchor is None
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])

    before = time.monotonic()
    result = await checker._replay_first_detect(dom_scan, keep_pages=True)
    after = time.monotonic()

    assert result == []
    assert dom_called == [], "first post-release empty poll must NOT scan"
    assert checker._dom_safety_net_anchor is not None, "anchor must be armed"
    assert before <= checker._dom_safety_net_anchor <= after


@pytest.mark.asyncio
async def test_empty_poll_with_elapsed_anchor_scans_and_reanchors(monkeypatch):
    """Anchor >=10s old and replay still empty → the replay-false-negative
    case the safety net exists for: scan, and re-anchor to now."""
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])
    stale_anchor = time.monotonic() - 10.5
    checker._dom_safety_net_anchor = stale_anchor

    result = await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert dom_called == [True], "scan must run once the interval elapsed"
    assert result == []
    assert checker._dom_safety_net_anchor > stale_anchor, (
        "a scan must re-anchor the throttle to now"
    )


@pytest.mark.asyncio
async def test_empty_poll_between_scans_is_throttled(monkeypatch):
    """Anchor fresh (<10s) → return [] fast with NO scan so the next replay
    poll runs at full cadence."""
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])
    fresh_anchor = time.monotonic() - 1.0
    checker._dom_safety_net_anchor = fresh_anchor

    result = await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert result == []
    assert dom_called == [], "scan must be throttled inside the 10s interval"
    assert checker._dom_safety_net_anchor == fresh_anchor, (
        "a throttled poll must NOT move the anchor"
    )


@pytest.mark.asyncio
async def test_replay_failure_bypasses_throttle(monkeypatch):
    """Replay None = fetch/decode FAILURE (not clean-empty): replay is broken,
    so DOM is the only detection — it must run even inside the interval."""
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, None)
    checker._dom_safety_net_anchor = time.monotonic() - 1.0  # throttle window

    await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert dom_called == [True], "replay failure must bypass the DOM throttle"


@pytest.mark.asyncio
async def test_replay_failure_scan_also_reanchors(monkeypatch):
    """A bypass scan is still a multi-second scan — it must re-anchor so a
    clean-empty poll right after it does not immediately scan again."""
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, None)
    stale_anchor = time.monotonic() - 60.0
    checker._dom_safety_net_anchor = stale_anchor

    await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert dom_called == [True]
    assert checker._dom_safety_net_anchor > stale_anchor


@pytest.mark.asyncio
async def test_successful_replay_detection_skips_dom_and_throttle(monkeypatch):
    """A replay HIT returns immediately — no DOM scan, throttle untouched
    (preserved fast-path behavior)."""
    checker = _make_checker()
    found = [_slot()]
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, found)
    monkeypatch.setattr(checker, "_record_slots", lambda *a, **k: None)

    result = await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert result == found
    assert dom_called == []
    assert checker._dom_safety_net_anchor is None, (
        "the fast path must not touch the throttle anchor"
    )


@pytest.mark.asyncio
async def test_non_sniper_path_not_throttled(monkeypatch):
    """keep_pages=False (non-sniper caller): the throttle is a sniper-hot-loop
    protection only — the DOM fallback still runs immediately."""
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])

    await checker._replay_first_detect(dom_scan, keep_pages=False)

    assert dom_called == [True]
    assert checker._dom_safety_net_anchor is None


@pytest.mark.asyncio
async def test_throttled_poll_logs_debug_not_info(monkeypatch, caplog):
    """The throttle-skip fires up to ~2x/sec — it must log at DEBUG. A scan
    that actually runs keeps its INFO line for drop forensics."""
    import logging
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])
    checker._dom_safety_net_anchor = time.monotonic() - 1.0

    with caplog.at_level(logging.DEBUG, logger="src.checker"):
        await checker._replay_first_detect(dom_scan, keep_pages=True)

    info_or_higher = [r for r in caplog.records
                      if r.levelno >= logging.INFO and "safety-net" in r.message]
    assert info_or_higher == [], "throttled skip must not log at INFO+"
    debug_lines = [r for r in caplog.records
                   if r.levelno == logging.DEBUG and "safety-net" in r.message]
    assert debug_lines, "throttled skip must leave a DEBUG trace"


@pytest.mark.asyncio
async def test_scan_that_runs_keeps_info_log(monkeypatch, caplog):
    """When the safety net actually scans post-release, the INFO line stays
    (drop forensics rely on it)."""
    import logging
    checker = _make_checker()
    dom_called, dom_scan = _wire_empty(checker, monkeypatch, [])
    checker._dom_safety_net_anchor = time.monotonic() - 11.0

    with caplog.at_level(logging.INFO, logger="src.checker"):
        await checker._replay_first_detect(dom_scan, keep_pages=True)

    assert dom_called == [True]
    info_lines = [r for r in caplog.records
                  if r.levelno == logging.INFO and "safety-net" in r.message]
    assert info_lines, "a DOM scan that runs must keep its INFO forensics line"
