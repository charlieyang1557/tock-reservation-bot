"""
Poll-rate watchdog.

Detects pathological poll bursts — e.g. the 2026-04-14 20:14 incident where
hundreds of polls fired in a 13-second window. Escalates: first/second trip
warns and throttles; third trip in escalation_window raises SystemExit so
the operator (or systemd) can restart the bot from a known state.

Two modes, swapped via ``enter_sniper_mode()`` / ``exit_sniper_mode()``:
  - Normal (default 10/5s): generous bound around the legacy ~1 poll/3-4s rate.
  - Sniper (default 60/5s): tuned for calendar-replay polls (~150 ms each,
    so 5-10/sec is healthy). The ceiling still catches a real tight loop
    (post-release pathological bursts), it just doesn't trip on every
    legitimate sniper cycle.

During an active sniper window the watchdog also suppresses the third-trip
``sys.exit(3)`` escalation — a ~14 s restart at the release boundary loses
the slot. Instead it logs ERROR, sleeps for ``sniper_escalation_throttle_sec``,
and lets the bot keep polling. The exit-on-third-trip policy still applies
outside sniper mode.
"""

import logging
import sys
import time
from collections import deque

logger = logging.getLogger(__name__)


class WatchdogTrip(Exception):
    """Raised by tick() when the burst threshold is crossed (caller throttles)."""


class PollWatchdog:
    """Detects pathological poll bursts and escalates on repeat offenses.

    Each ``tick()`` records the current time. If the active burst threshold
    falls within the last ``window_sec`` seconds, ``tick()`` raises
    ``WatchdogTrip`` and sleeps the active throttle to break tight loops in
    the caller. The third trip within ``escalation_window_sec`` calls
    ``sys.exit(3)`` outside sniper mode; inside sniper mode it logs ERROR
    and applies a longer throttle instead.
    """

    def __init__(
        self,
        burst_threshold: int = 10,
        window_sec: float = 5.0,
        escalation_window_sec: float = 60.0,
        throttle_sec: float = 2.0,
        sniper_burst_threshold: int = 60,
        sniper_escalation_throttle_sec: float = 5.0,
    ) -> None:
        self._normal_burst_threshold = burst_threshold
        self._sniper_burst_threshold = sniper_burst_threshold
        self._burst_threshold = burst_threshold
        self._window_sec = window_sec
        self._escalation_window_sec = escalation_window_sec
        self._throttle_sec = throttle_sec
        self._sniper_escalation_throttle_sec = sniper_escalation_throttle_sec
        self._sniper_active = False
        # maxlen=256 holds >5s of timestamps at the highest sniper-mode rate
        # (60 polls / 5s headroom plus a margin), so the window-truncation
        # loop in tick() never fights the deque's bounded capacity.
        self._timestamps: deque[float] = deque(maxlen=256)
        self._trip_times: deque[float] = deque()  # times of WatchdogTrip

    @property
    def trip_count(self) -> int:
        """Number of trips currently inside the escalation window."""
        return len(self._trip_times)

    def reset_rolling(self) -> None:
        """Clear the rolling timestamp deque (used by tests + after throttling)."""
        self._timestamps.clear()

    def _reset_state(self) -> None:
        # Trip history is mode-specific (sniper-rate trips are by design;
        # normal-mode trips are anomalies). Cleared on every transition so
        # trips don't bleed across regimes — a suppressed sniper trip must
        # not, e.g., become a sys.exit on the first post-window normal trip.
        self._timestamps.clear()
        self._trip_times.clear()

    def enter_sniper_mode(self) -> None:
        """Swap in the higher sniper burst threshold and suppress sys.exit."""
        if self._sniper_active:
            return
        self._sniper_active = True
        self._burst_threshold = self._sniper_burst_threshold
        self._reset_state()
        logger.info(
            f"[monitor] Watchdog -> sniper mode "
            f"(burst threshold {self._burst_threshold}/{self._window_sec:.0f}s, "
            f"sys.exit suppressed)"
        )

    def exit_sniper_mode(self) -> None:
        """Restore the normal (tight) burst threshold and sys.exit policy."""
        if not self._sniper_active:
            return
        self._sniper_active = False
        self._burst_threshold = self._normal_burst_threshold
        self._reset_state()
        logger.info(
            f"[monitor] Watchdog -> normal mode "
            f"(burst threshold {self._burst_threshold}/{self._window_sec:.0f}s)"
        )

    def tick(self) -> None:
        """Record one poll. Raises WatchdogTrip on burst; SystemExit on 3rd trip in window."""
        now = time.monotonic()
        self._timestamps.append(now)

        # Drop timestamps outside the rolling window
        while self._timestamps and now - self._timestamps[0] > self._window_sec:
            self._timestamps.popleft()

        # Drop trips outside the escalation window
        while self._trip_times and now - self._trip_times[0] > self._escalation_window_sec:
            self._trip_times.popleft()

        if len(self._timestamps) < self._burst_threshold:
            return

        # Burst detected
        self._trip_times.append(now)
        recent_count = len(self._timestamps)

        logger.warning(
            f"[monitor] Poll-rate watchdog triggered: "
            f"{recent_count} polls in {self._window_sec:.0f}s "
            f"(trip {self.trip_count} of 3 within {self._escalation_window_sec:.0f}s, "
            f"{'sniper' if self._sniper_active else 'normal'} mode)"
        )

        if self.trip_count >= 3:
            if self._sniper_active:
                # Cure-worse-than-disease guard: a ~14 s restart at the
                # release boundary is catastrophic. Log loud, sleep longer
                # than the regular throttle, keep polling.
                logger.error(
                    f"[monitor] Poll-rate watchdog escalation during sniper window: "
                    f"3 trips in {self._escalation_window_sec:.0f}s. "
                    f"Throttling {self._sniper_escalation_throttle_sec:.1f}s and "
                    "continuing (sys.exit suppressed to protect the slot)."
                )
                time.sleep(self._sniper_escalation_throttle_sec)
                self.reset_rolling()
                raise WatchdogTrip(
                    f"{recent_count} polls in {self._window_sec:.0f}s — "
                    "sniper escalation throttle"
                )
            logger.error(
                f"[monitor] Poll-rate watchdog escalation: "
                f"3 trips in {self._escalation_window_sec:.0f}s — exiting non-zero. "
                "Restart the bot to recover."
            )
            sys.exit(3)

        # Throttle to break any tight loop in the caller
        time.sleep(self._throttle_sec)
        self.reset_rolling()
        raise WatchdogTrip(
            f"{recent_count} polls in {self._window_sec:.0f}s — throttled"
        )
