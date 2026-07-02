"""Tests for the 'total calendar failure' Discord alert (06/19 incident class).

Background
----------
On Fri 2026-06-19 the sniper window ran for ~10 minutes during which EVERY
calendar load timed out (72 SELECTOR_FAILED, all dates errored) and the bot
reported 0 slots. That looked identical, to the operator, to a benign quiet
night — because the only signal for an empty window was the GREY
``sniper_mode_ended`` summary, and the per-poll 0-slot path is console-only.

These tests pin the fix: a poll that returns 0 slots because EVERY date
ERRORED (``last_errors == last_checks > 0``) must fire ONE red Discord alert
per sniper window, distinct from the benign 'released-but-sold-out / no
release' state (0 errors, 0 slots), which must stay silent.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

from src.config import Config
from src.monitor import TockMonitor
from src.notifier import Notifier, _RED


# ---------------------------------------------------------------------------
# Builders (mirrors tests/test_monitor_sniper.py)
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="test@example.com",
        tock_password="pass",
        card_cvc="123",
        discord_webhook_url="",
        headless=True,
        dry_run=True,
        restaurant_slug="test-restaurant",
        party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=["Monday", "Tuesday", "Wednesday", "Thursday"],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Wednesday", "Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_monitor(**config_overrides) -> TockMonitor:
    cfg = _make_config(**config_overrides)
    browser = MagicMock()
    browser.warm_session = AsyncMock()

    checker = MagicMock()
    checker.check_all = AsyncMock(return_value=[])
    checker.close_sniper_pages = AsyncMock()
    checker.close_replay_session = AsyncMock()
    checker.close_handoff_pages = AsyncMock()
    checker.last_checks = 0
    checker.last_errors = 0

    notifier = MagicMock()
    tracker = MagicMock()

    monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    return monitor


def _set_counts(monitor, *, errors: int, checks: int) -> None:
    monitor.checker.last_errors = errors
    monitor.checker.last_checks = checks


# ---------------------------------------------------------------------------
# Notifier method
# ---------------------------------------------------------------------------

def test_notifier_sniper_calendar_failure_fires_red_embed_with_counts():
    """notifier.sniper_calendar_failure() fires a RED embed naming the
    errored/total date counts."""
    notifier = Notifier(_make_config())
    notifier._fire = MagicMock()

    notifier.sniper_calendar_failure(errors=6, checks=6)

    notifier._fire.assert_called_once()
    kwargs = notifier._fire.call_args.kwargs
    assert kwargs["color"] == _RED
    assert "6/6" in kwargs["description"] or "6/6" in kwargs["title"]


# ---------------------------------------------------------------------------
# Monitor gating (the helper)
# ---------------------------------------------------------------------------

def test_total_calendar_failure_fires_alert_once():
    """Every date errored, post-release → fire exactly one alert."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=6, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=120.0)

    m.notifier.sniper_calendar_failure.assert_called_once_with(6, 6)


def test_total_calendar_failure_deduped_within_window():
    """A second total-failure poll in the same window does NOT re-alert."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=6, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=120.0)
    m._maybe_alert_calendar_failure(sniper_age=121.0)

    m.notifier.sniper_calendar_failure.assert_called_once()


def test_benign_empty_no_release_does_not_alert():
    """0 errors + 0 slots = released-but-sold-out / no release → SILENT."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=0, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=120.0)

    m.notifier.sniper_calendar_failure.assert_not_called()


def test_partial_errors_does_not_alert():
    """Some dates loaded fine (errors < checks) → not a total failure."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=3, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=120.0)

    m.notifier.sniper_calendar_failure.assert_not_called()


def test_outside_sniper_window_does_not_alert():
    """Total errors outside a sniper window must not alert (normal polls
    legitimately fail sometimes and have their own handling)."""
    m = _make_monitor()
    m._sniper_active = False
    _set_counts(m, errors=6, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=None)

    m.notifier.sniper_calendar_failure.assert_not_called()


def test_pre_release_total_errors_does_not_alert():
    """Errors in the first 60s (pre-release, slots not dropped yet) are
    expected and must not alert."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=6, checks=6)

    m._maybe_alert_calendar_failure(sniper_age=30.0)

    m.notifier.sniper_calendar_failure.assert_not_called()


def test_alert_rearms_on_new_sniper_window():
    """The dedup flag resets when a fresh sniper window is entered, so a
    failure in a later window alerts again."""
    m = _make_monitor()
    m._sniper_active = True
    _set_counts(m, errors=6, checks=6)
    m._maybe_alert_calendar_failure(sniper_age=120.0)

    # Simulate window end + a new window entry re-arming the dedup flag.
    m._sniper_calendar_failure_alerted = False

    m._maybe_alert_calendar_failure(sniper_age=120.0)
    assert m.notifier.sniper_calendar_failure.call_count == 2


# ---------------------------------------------------------------------------
# Wiring: the 0-slot branch of _poll_inner actually calls the helper
# ---------------------------------------------------------------------------

def test_poll_inner_zero_slots_total_failure_alerts():
    """Driving _poll_inner with check_all → [] and all-errored counters
    fires the calendar-failure alert (proves the branch is wired)."""
    m = _make_monitor()
    m._sniper_active = True
    m.checker.check_all = AsyncMock(return_value=[])
    _set_counts(m, errors=6, checks=6)

    asyncio.run(m._poll_inner(sniper_age=120.0))

    m.notifier.sniper_calendar_failure.assert_called_once_with(6, 6)
