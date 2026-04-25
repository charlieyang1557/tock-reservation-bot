"""
Poll-rate watchdog.

Detects pathological poll bursts — e.g. the 2026-04-14 20:14 incident where
hundreds of polls fired in a 13-second window. Escalates: first/second trip
warns and throttles; third trip in escalation_window raises SystemExit so
the operator (or systemd) can restart the bot from a known state.

The threshold (default 10 polls in 5s) is an order of magnitude above any
legitimate sniper-mode rate (~1 poll per 3-4s) and well below pathological
bursts seen in the field.
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

    Each ``tick()`` records the current time. If ``burst_threshold`` ticks
    fall within the last ``window_sec`` seconds, ``tick()`` raises
    ``WatchdogTrip`` and sleeps ``throttle_sec`` to break tight loops in
    the caller. The third trip within ``escalation_window_sec`` calls
    ``sys.exit(3)`` so the operator (or process supervisor) can restart
    from a clean state.
    """

    def __init__(
        self,
        burst_threshold: int = 10,
        window_sec: float = 5.0,
        escalation_window_sec: float = 60.0,
        throttle_sec: float = 2.0,
    ) -> None:
        self._burst_threshold = burst_threshold
        self._window_sec = window_sec
        self._escalation_window_sec = escalation_window_sec
        self._throttle_sec = throttle_sec
        self._timestamps: deque[float] = deque(maxlen=64)
        self._trip_times: deque[float] = deque()  # times of WatchdogTrip

    @property
    def trip_count(self) -> int:
        """Number of trips currently inside the escalation window."""
        return len(self._trip_times)

    def reset_rolling(self) -> None:
        """Clear the rolling timestamp deque (used by tests + after throttling)."""
        self._timestamps.clear()

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
            f"(trip {self.trip_count} of 3 within {self._escalation_window_sec:.0f}s)"
        )

        if self.trip_count >= 3:
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
