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

import asyncio
from datetime import datetime, time
from unittest.mock import AsyncMock, MagicMock, patch

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
    checker.check_all = AsyncMock(return_value=[])
    checker.last_checks = 0
    checker.last_errors = 0
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
        """The monitor's pre-release window must match the threshold checker
        applies in `check_all()` (the `if keep_pages and sniper_window_age_sec
        < 60.0` gate). Drift = silent regression."""
        assert _SNIPER_PRE_RELEASE_SEC == 60.0


# ---------------------------------------------------------------------------
# Fix B — pre-release polls must not tick the watchdog.
#
# Background: checker.check_all() short-circuits when age < 60s (no I/O at
# all). The watchdog should treat those polls as no-ops so the pre-release
# tick storm doesn't burn the 3-trip budget. Once we cross the release
# boundary, the watchdog must re-arm and tick every real poll.
# ---------------------------------------------------------------------------

class TestWatchdogSkipsPreReleasePolls:
    def _force_window(self, monitor: TockMonitor, age_sec: float) -> None:
        """Monkey-patch so the monitor thinks it's age_sec into the sniper window."""
        monitor._sniper_active = True
        monitor._current_sniper_age_sec = MagicMock(return_value=age_sec)

    def test_pre_release_poll_does_not_tick_watchdog(self):
        """During pre-release (age < 60s), poll() must not tick the watchdog."""
        m = _make_monitor()
        self._force_window(m, age_sec=10.0)
        m._watchdog = MagicMock()
        # _watchdog.tick must be a regular MagicMock so call_count is observable
        m._watchdog.tick = MagicMock()
        asyncio.run(m.poll())
        assert m._watchdog.tick.call_count == 0, (
            "Pre-release polls produce zero I/O — watchdog must not tick"
        )

    def test_post_release_poll_ticks_watchdog(self):
        """At age >= 60s the watchdog must tick (real work is happening)."""
        m = _make_monitor()
        self._force_window(m, age_sec=120.0)
        m._watchdog = MagicMock()
        m._watchdog.tick = MagicMock()
        asyncio.run(m.poll())
        assert m._watchdog.tick.call_count == 1

    def test_non_sniper_poll_ticks_watchdog(self):
        """Normal-mode (non-sniper) polls must tick — that's the original
        invariant the watchdog was designed for."""
        m = _make_monitor()
        m._sniper_active = False
        m._watchdog = MagicMock()
        m._watchdog.tick = MagicMock()
        asyncio.run(m.poll())
        assert m._watchdog.tick.call_count == 1

    def test_enter_sniper_mode_called_once_per_transition(self):
        """enter_sniper_mode must fire only on the False→True edge, not on
        every poll while the window is open (the transition contract)."""
        m = _make_monitor()
        m._watchdog = MagicMock()

        m._sniper_active = False
        with patch.object(m, "_sniper_window_info", return_value="20:10"):
            m._get_poll_interval()  # transition
            m._get_poll_interval()  # already in sniper — no re-entry
            m._get_poll_interval()
        assert m._watchdog.enter_sniper_mode.call_count == 1
        assert m._watchdog.exit_sniper_mode.call_count == 0

    def test_exit_sniper_mode_called_once_per_transition(self):
        """exit_sniper_mode must fire only on the True→False edge."""
        m = _make_monitor()
        m._watchdog = MagicMock()

        m._sniper_active = True
        with patch.object(m, "_sniper_window_info", return_value=None):
            m._get_poll_interval()
            m._get_poll_interval()
            m._get_poll_interval()
        assert m._watchdog.exit_sniper_mode.call_count == 1
        assert m._watchdog.enter_sniper_mode.call_count == 0


class TestRunAdaptiveTestWiring:
    """run_adaptive_test claims to run "the same code that runs during a live
    sniper window" — that's only true if it flips the watchdog too."""

    def test_adaptive_test_enters_and_exits_sniper_on_watchdog(self):
        m = _make_monitor()
        m._watchdog = MagicMock()
        # Bypass real poll() — we only care that the wrapping wires the watchdog.
        m.poll = AsyncMock()
        asyncio.run(m.run_adaptive_test(num_polls=1))
        m._watchdog.enter_sniper_mode.assert_called_once()
        m._watchdog.exit_sniper_mode.assert_called_once()

    def test_adaptive_test_exits_sniper_on_poll_exception(self):
        """If poll() raises mid-loop, the watchdog must still be restored —
        otherwise sniper config leaks into the rest of the process."""
        m = _make_monitor()
        m._watchdog = MagicMock()
        m.poll = AsyncMock(side_effect=RuntimeError("boom"))
        with pytest.raises(RuntimeError):
            asyncio.run(m.run_adaptive_test(num_polls=1))
        m._watchdog.exit_sniper_mode.assert_called_once()


# ---------------------------------------------------------------------------
# Two-clock race: poll() and check_all() must agree on whether this poll is
# pre-release. If poll() reads now=59.9s and decides "skip watchdog", but
# _poll_inner reads now=60.1s and check_all does real I/O, we get a real-I/O
# poll that bypassed the watchdog (or vice versa: a no-I/O poll that ticked).
# ---------------------------------------------------------------------------

class TestSniperAgeReadOncePerPoll:
    def test_age_computed_once_and_piped_to_check_all(self):
        """poll() must read the sniper age exactly once AND forward that same
        value to check_all — guards against both re-reads and stale-default
        forwarding regressions."""
        m = _make_monitor()
        m._sniper_active = True
        m._watchdog = MagicMock()

        # Make _current_sniper_age_sec return different values on each call —
        # if poll() reads it twice (once for skip, once via _poll_inner), the
        # two paths land on opposite sides of the 60s boundary.
        age_iter = iter([59.0, 65.0, 70.0, 75.0])
        m._current_sniper_age_sec = MagicMock(side_effect=lambda *_: next(age_iter))
        m.checker.check_all = AsyncMock(return_value=[])
        m.checker.last_checks = 1
        m.checker.last_errors = 0

        asyncio.run(m.poll())

        assert m._current_sniper_age_sec.call_count == 1, (
            f"sniper age was read {m._current_sniper_age_sec.call_count}× — "
            "must be cached at poll() entry so skip-decision and check_all agree"
        )
        # The cached pre-release age must flow into check_all unchanged.
        # 59.0 is pre-release, so the watchdog must have skipped AND
        # check_all must have received the same 59.0 (not a recomputed value).
        assert m._watchdog.tick.call_count == 0, "Pre-release: watchdog must skip"
        _, kwargs = m.checker.check_all.call_args
        assert kwargs["sniper_window_age_sec"] == 59.0, (
            f"check_all got {kwargs['sniper_window_age_sec']} but the cached "
            "pre-release age was 59.0 — values must agree"
        )

    def test_post_release_poll_passes_post_release_age_to_check_all(self):
        """When poll() decides post-release (no watchdog skip), check_all must
        receive a post-release age — not a recomputed pre-release one."""
        m = _make_monitor()
        m._sniper_active = True
        m._watchdog = MagicMock()
        # Single value, but the test pins down that check_all sees it.
        m._current_sniper_age_sec = MagicMock(return_value=120.0)

        m.checker.check_all = AsyncMock(return_value=[])
        m.checker.last_checks = 1
        m.checker.last_errors = 0

        asyncio.run(m.poll())

        # Watchdog ticked (post-release)
        assert m._watchdog.tick.call_count == 1
        # check_all received the same age
        _, kwargs = m.checker.check_all.call_args
        assert kwargs["sniper_window_age_sec"] == 120.0
