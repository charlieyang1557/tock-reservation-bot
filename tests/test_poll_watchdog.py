"""Tests for the poll-rate watchdog escalation policy."""
import time
import pytest

from src.poll_watchdog import PollWatchdog, WatchdogTrip


def _drain(watchdog: PollWatchdog, n: int, interval_s: float = 0.0) -> int:
    """Tick the watchdog n times. Return number of trips raised."""
    trips = 0
    for _ in range(n):
        try:
            watchdog.tick()
        except WatchdogTrip:
            trips += 1
        if interval_s:
            time.sleep(interval_s)
    return trips


def test_normal_rate_no_trip():
    """Normal poll rate (1 tick per ~3s) must not trip."""
    watchdog = PollWatchdog(burst_threshold=10, window_sec=5.0)
    trips = _drain(watchdog, 5, interval_s=0.6)  # 5 ticks over 3s
    assert trips == 0


def test_burst_above_threshold_trips():
    """≥10 ticks within 5 seconds must trip the watchdog."""
    watchdog = PollWatchdog(burst_threshold=10, window_sec=5.0)
    trips = _drain(watchdog, 15)  # 15 immediate ticks
    assert trips >= 1, f"Expected ≥1 trip from burst of 15, got {trips}"


def test_third_trip_within_60s_escalates_to_exit():
    """Three trips within 60s must raise SystemExit (escalation policy)."""
    watchdog = PollWatchdog(burst_threshold=5, window_sec=2.0, escalation_window_sec=60.0)

    # First trip: warn + throttle
    for _ in range(10):
        try:
            watchdog.tick()
        except WatchdogTrip:
            break
    assert watchdog.trip_count == 1

    # Second trip
    time.sleep(0.1)
    for _ in range(10):
        try:
            watchdog.tick()
        except WatchdogTrip:
            break
    assert watchdog.trip_count == 2

    # Third trip — must escalate
    time.sleep(0.1)
    with pytest.raises(SystemExit):
        for _ in range(10):
            watchdog.tick()


def test_old_trips_age_out():
    """Trips older than escalation_window must not count toward escalation."""
    watchdog = PollWatchdog(burst_threshold=5, window_sec=2.0, escalation_window_sec=0.5)

    # Trip once
    for _ in range(10):
        try:
            watchdog.tick()
        except WatchdogTrip:
            break
    assert watchdog.trip_count == 1

    # Wait past the escalation window
    time.sleep(0.6)

    # Reset rolling deque
    watchdog.reset_rolling()

    # Trip again — should be counted as the first, not third
    for _ in range(10):
        try:
            watchdog.tick()
        except WatchdogTrip:
            break
    assert watchdog.trip_count == 1
