"""
Tests for pre-release sniper sleep — guards against the 2026-05-01 19:44 spin loop.

Bug: When sniper mode entered with sniper_age < 60s, checker.check_all() returned
immediately (no I/O) because of the pre-release skip. The run loop then awaited
asyncio.sleep(0), causing polls to fire at machine speed and tripping
PollWatchdog.escalation (3 trips in 60s → sys.exit(3) → PM2 restart loop).

Evidence from bot.log: 120 "Poll #... (next in 0s)" lines in the 19:44–19:45
window with no calendar I/O. 4 watchdog trips in 50s (19:44:09/27/44/57).

Fix: monitor must sleep across the pre-release boundary instead of spinning
at sleep(0). Once age >= 60s, the page-load latency rate-limits naturally.
"""

from datetime import datetime, time
from unittest.mock import MagicMock, patch

import pytest

from src.config import Config
from src.monitor import (
    PT,
    TockMonitor,
    _SNIPER_PRE_RELEASE_SEC,
    _SNIPER_PRE_RELEASE_TICK_SEC,
)


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@example.com",
        tock_password="p",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=["Monday", "Tuesday", "Wednesday", "Thursday"],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:44"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_monitor(config=None) -> TockMonitor:
    cfg = config or _make_config()
    browser = MagicMock()
    checker = MagicMock()
    notifier = MagicMock()
    tracker = MagicMock()
    with patch("src.monitor.TockBooker"):
        return TockMonitor(cfg, browser, checker, notifier, tracker)


def _pt(year, month, day, hour, minute, second=0) -> datetime:
    return PT.localize(datetime(year, month, day, hour, minute, second))


# ---------------------------------------------------------------------------
# _current_sniper_age_sec
# ---------------------------------------------------------------------------

class TestCurrentSniperAgeSec:
    def test_returns_zero_at_window_start(self):
        m = _make_monitor()
        # Friday 2026-05-01 19:44:00 — exact start of window
        assert m._current_sniper_age_sec(_pt(2026, 5, 1, 19, 44, 0)) == 0.0

    def test_returns_seconds_into_window(self):
        m = _make_monitor()
        # 30s into the window
        age = m._current_sniper_age_sec(_pt(2026, 5, 1, 19, 44, 30))
        assert age == 30.0

    def test_returns_none_before_window(self):
        m = _make_monitor()
        assert m._current_sniper_age_sec(_pt(2026, 5, 1, 19, 43, 59)) is None

    def test_returns_none_after_window(self):
        m = _make_monitor()
        # Window: 19:44 + 11min = 19:55. 19:55:01 is past end.
        assert m._current_sniper_age_sec(_pt(2026, 5, 1, 19, 55, 1)) is None

    def test_returns_none_wrong_day(self):
        m = _make_monitor()
        # Thursday 2026-04-30 — not a sniper day
        assert m._current_sniper_age_sec(_pt(2026, 4, 30, 19, 44, 30)) is None

    def test_picks_correct_window_with_multiple_times(self):
        m = _make_monitor(_make_config(sniper_times=["16:59", "19:44"]))
        # In the second window, 90s in
        age = m._current_sniper_age_sec(_pt(2026, 5, 1, 19, 45, 30))
        assert age == 90.0


# ---------------------------------------------------------------------------
# _pre_release_sleep_sec
# ---------------------------------------------------------------------------

class TestPreReleaseSleepSec:
    def test_returns_positive_during_pre_release(self):
        """At age=10s (50s before release), should sleep > 0."""
        m = _make_monitor()
        sleep = m._pre_release_sleep_sec(_pt(2026, 5, 1, 19, 44, 10))
        assert sleep > 0
        assert sleep <= _SNIPER_PRE_RELEASE_TICK_SEC

    def test_returns_zero_at_release_boundary(self):
        m = _make_monitor()
        # Exactly 60s in — release happens now
        assert m._pre_release_sleep_sec(_pt(2026, 5, 1, 19, 45, 0)) == 0.0

    def test_returns_zero_post_release(self):
        m = _make_monitor()
        assert m._pre_release_sleep_sec(_pt(2026, 5, 1, 19, 46, 0)) == 0.0

    def test_returns_zero_outside_sniper(self):
        m = _make_monitor()
        assert m._pre_release_sleep_sec(_pt(2026, 5, 1, 19, 30, 0)) == 0.0

    def test_caps_at_tick_interval(self):
        """Even when 59s remain to release, single sleep is bounded by tick."""
        m = _make_monitor()
        sleep = m._pre_release_sleep_sec(_pt(2026, 5, 1, 19, 44, 1))
        # 59s remain to release, but we cap at TICK so the loop wakes
        # frequently enough to cross the release boundary promptly.
        assert sleep == _SNIPER_PRE_RELEASE_TICK_SEC

    def test_pre_release_sec_constant_matches_checker_skip(self):
        """The monitor's pre-release window must match checker.py's skip
        threshold (currently 60s in checker.py:406). Drift = silent regression."""
        assert _SNIPER_PRE_RELEASE_SEC == 60.0
