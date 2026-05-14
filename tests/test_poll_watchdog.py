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
    watchdog = PollWatchdog(burst_threshold=10, window_sec=5.0, throttle_sec=0.0)
    trips = _drain(watchdog, 15)  # 15 immediate ticks
    assert trips >= 1, f"Expected ≥1 trip from burst of 15, got {trips}"


def test_third_trip_within_60s_escalates_to_exit():
    """Three trips within 60s must raise SystemExit (escalation policy)."""
    watchdog = PollWatchdog(
        burst_threshold=5, window_sec=2.0,
        escalation_window_sec=60.0, throttle_sec=0.0,
    )

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
    watchdog = PollWatchdog(
        burst_threshold=5, window_sec=2.0,
        escalation_window_sec=0.5, throttle_sec=0.0,
    )

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


# ---------------------------------------------------------------------------
# Fix A — sniper-aware burst threshold.
#
# Background: calendar-replay made polls ~150ms (vs ~3-4s pre-replay), so a
# legitimate sniper window now produces 5-10 polls/sec. The old 10/5s burst
# limit trips every cycle. Sniper mode must accept a much higher burst,
# while normal-mode thresholds stay tight.
# ---------------------------------------------------------------------------

class TestSniperAwareThreshold:
    def test_sniper_mode_allows_higher_burst(self):
        """Entering sniper mode lifts the burst limit above the normal threshold."""
        wd = PollWatchdog(burst_threshold=10, window_sec=5.0, throttle_sec=0.0)
        wd.enter_sniper_mode()
        # 30 immediate ticks would trip normal config (10/5s) but must not
        # trip sniper config (default 60/5s).
        trips = _drain(wd, 30)
        assert trips == 0

    def test_exit_sniper_restores_normal_threshold(self):
        """After exit, the original (low) burst threshold is back in force."""
        wd = PollWatchdog(burst_threshold=10, window_sec=5.0, throttle_sec=0.0)
        wd.enter_sniper_mode()
        # Tick a lot in sniper — fine
        _drain(wd, 30)
        wd.exit_sniper_mode()
        # The rolling deque is reset on exit so normal mode starts clean
        trips = _drain(wd, 15)
        assert trips >= 1, "Normal threshold must still trip after exit_sniper_mode"

    def test_sniper_threshold_eventually_trips_on_pathological_burst(self):
        """Even sniper has an upper bound — a runaway tight loop still trips."""
        wd = PollWatchdog(
            burst_threshold=10, window_sec=5.0, throttle_sec=0.0,
            sniper_burst_threshold=60,
        )
        wd.enter_sniper_mode()
        trips = _drain(wd, 200)  # well above 60/5s ceiling
        assert trips >= 1, "Sniper burst must still trip on pathological loops"

    def test_sniper_threshold_configurable_via_ctor(self):
        """sniper_burst_threshold ctor arg overrides the default."""
        wd = PollWatchdog(
            burst_threshold=10, window_sec=5.0, throttle_sec=0.0,
            sniper_burst_threshold=20,
        )
        wd.enter_sniper_mode()
        # 25 ticks > sniper threshold of 20 → must trip
        trips = _drain(wd, 25)
        assert trips >= 1


# ---------------------------------------------------------------------------
# Fix C — no sys.exit during active sniper window.
#
# When sniper is active, a 14-sec restart at the release boundary loses the
# slot. Watchdog must log loudly, increase throttle, and continue.
# ---------------------------------------------------------------------------

class TestNoExitDuringSniper:
    def _trip(self, wd: PollWatchdog) -> None:
        for _ in range(10):
            try:
                wd.tick()
            except WatchdogTrip:
                return

    def test_third_trip_during_sniper_does_not_exit(self):
        """3 trips inside escalation window while sniper-active must NOT sys.exit."""
        wd = PollWatchdog(
            burst_threshold=5, window_sec=2.0,
            escalation_window_sec=60.0, throttle_sec=0.0,
            sniper_burst_threshold=5,  # match normal so test can force trips
        )
        wd.enter_sniper_mode()

        self._trip(wd)
        assert wd.trip_count == 1
        time.sleep(0.05)
        self._trip(wd)
        assert wd.trip_count == 2
        time.sleep(0.05)
        # Third trip — would sys.exit in normal mode; must not while sniper-active
        try:
            for _ in range(10):
                wd.tick()
        except WatchdogTrip:
            pass
        except SystemExit:  # pragma: no cover — failure mode
            pytest.fail("Watchdog must not sys.exit during an active sniper window")
        assert wd.trip_count == 3

    def test_third_trip_uses_higher_throttle_during_sniper(self):
        """Third sniper-mode trip should sleep longer than normal throttle."""
        normal_throttle = 0.1
        wd = PollWatchdog(
            burst_threshold=5, window_sec=2.0,
            escalation_window_sec=60.0, throttle_sec=normal_throttle,
            sniper_burst_threshold=5,
            sniper_escalation_throttle_sec=0.5,
        )
        wd.enter_sniper_mode()

        self._trip(wd)
        time.sleep(0.05)
        self._trip(wd)
        time.sleep(0.05)
        # Trip #3 — measure sleep
        start = time.monotonic()
        try:
            for _ in range(10):
                wd.tick()
        except WatchdogTrip:
            pass
        elapsed = time.monotonic() - start
        # Must sleep at least the escalated throttle
        assert elapsed >= 0.4, f"Expected escalated throttle ~0.5s, got {elapsed:.3f}s"

    def test_sniper_trips_do_not_carry_into_normal_mode_exit(self):
        """Suppressed sniper-mode trips must not count toward post-window
        sys.exit — they were tolerated *because* sniper rates are by design.
        After exit_sniper_mode, the trip counter starts fresh."""
        wd = PollWatchdog(
            burst_threshold=5, window_sec=2.0,
            escalation_window_sec=60.0, throttle_sec=0.0,
            sniper_burst_threshold=5,
        )
        wd.enter_sniper_mode()
        # 2 sniper trips (3rd would be suppressed, but we stop at 2)
        self._trip(wd)
        time.sleep(0.05)
        self._trip(wd)
        assert wd.trip_count == 2
        wd.exit_sniper_mode()
        # Trip counter must reset on mode transition
        assert wd.trip_count == 0
        # One more trip in normal mode = trip 1, not trip 3 — must not exit
        time.sleep(0.05)
        try:
            for _ in range(10):
                wd.tick()
        except WatchdogTrip:
            pass
        except SystemExit:  # pragma: no cover — failure mode
            pytest.fail(
                "Sniper-era trips leaked into normal-mode escalation count"
            )
        assert wd.trip_count == 1

    def test_post_sniper_normal_mode_still_escalates_after_3_fresh_trips(self):
        """The exit policy still works post-sniper — it just needs 3 *normal-mode*
        trips, not 3 sniper-mode trips."""
        wd = PollWatchdog(
            burst_threshold=5, window_sec=2.0,
            escalation_window_sec=60.0, throttle_sec=0.0,
            sniper_burst_threshold=5,
        )
        wd.enter_sniper_mode()
        self._trip(wd)
        self._trip(wd)
        wd.exit_sniper_mode()
        time.sleep(0.05)
        self._trip(wd)
        time.sleep(0.05)
        self._trip(wd)
        time.sleep(0.05)
        with pytest.raises(SystemExit):
            for _ in range(10):
                wd.tick()
