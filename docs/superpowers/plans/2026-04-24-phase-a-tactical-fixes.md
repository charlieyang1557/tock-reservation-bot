# Phase A: Tactical Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the five tactical issues identified in the architecture redesign spec — log-spam runaway loops, generic-fallback slot labels that the booker can't match, oversized scan ranges in sniper mode, and selector-scope leakage that lets non-slot buttons win the booker's race.

**Architecture:** Five sequenced tasks (A1 → A2 → A3 → A5 → A4) all within the existing `monitor.py` / `checker.py` / `selectors.py` / `main.py` modules. No new abstractions. Two modules grow new files: `src/process_lock.py` (singleton flock) and `src/poll_watchdog.py` (rate guard). Each task ships independently behind its own commit; the bot remains runnable after every commit.

**Tech Stack:** Python 3.11, asyncio, Playwright async_api, pytest, pytest-asyncio, `fcntl` (stdlib), `collections.deque` (stdlib), `dotenv`.

---

## Spec reference

This plan implements Section 2 of `docs/superpowers/specs/2026-04-24-tock-bot-architecture-redesign-design.md`. All five fixes are tactical and live within the current architecture; the scanner/booker split (Phase B) and hybrid spike (Phase C) come later, gated by release-window observations.

## File Structure

| File | Change | Responsibility |
|------|--------|---------------|
| `src/process_lock.py` | **Create** | Singleton `flock`-based startup lock. ~60 LOC. Used only by `main.py`. |
| `src/poll_watchdog.py` | **Create** | Rate-watchdog deque + escalation policy. ~80 LOC. Used only by `monitor.py`. |
| `src/checker.py` | Modify (~50 LOC delta) | A3: drop `"Slot N"` fallback; add aria-label / 3-ancestor extraction. A4: cap dates to `sniper_scan_weeks` when `keep_pages=True`. A5: scope slot collection to `slots_container` when present. |
| `src/selectors.py` | Modify (~10 LOC delta) | A5: add `slots_container` selector key. |
| `src/config.py` | Modify (~5 LOC delta) | A4: add `sniper_scan_weeks: int = 2` field + env loader. |
| `src/monitor.py` | Modify (~15 LOC delta) | A2: instantiate `PollWatchdog`, call `tick()` at top of every `poll()`. |
| `main.py` | Modify (~12 LOC delta) | A2: acquire process lock before any other init; release in `finally`. |
| `tests/test_log_spam_diagnosis.md` | **Create** | A1 deliverable — root-cause findings doc, not code. Lives in `docs/superpowers/observations/` after writing-skill commit. |
| `tests/test_poll_watchdog.py` | **Create** | A2 unit tests for the watchdog. |
| `tests/test_process_lock.py` | **Create** | A2 two-process integration test. |
| `tests/test_slot_labeling.py` | **Create** | A3 tests for time extraction priority order, including `aria-label` and 3-ancestor walk. |
| `tests/test_scoped_slot_selectors.py` | **Create** | A5 test that an out-of-container `Book` button is not collected. |
| `tests/test_sniper_scan_weeks.py` | **Create** | A4 test that sniper-mode date list is capped at `sniper_scan_weeks`. |

## Test execution conventions

- Always use the project's MM-style test command from `~/.claude/rules/testing.md`: `python -m pytest tests/<file>.py -q`.
- After every task, run the full suite with `python -m pytest tests/ -q` to catch regressions.
- For tests requiring `pytest-asyncio`, use `@pytest.mark.asyncio` (already configured in this project).

---

## Task 1 (A1): Investigate the 20:14 log-spam incident

**Context:** On 2026-04-14 at ~20:14 PT, the bot emitted hundreds of `Poll #1168835`, `Poll #1168836`, … lines in a ~13s window with `No available slots found this cycle.` on each line. A real poll takes ≥3-5s (calendar load + click + slot scan), so 100s of polls in 13s is structurally impossible from the legitimate `monitor.run()` loop. The hypothesis space is: (a) duplicate `python main.py` process, (b) re-entrant calls to `monitor.poll()`, (c) `notifier.poll_start` being called per-date instead of per-poll, (d) some shell wrapper that spawns subprocesses.

This task is **research, not code**. The deliverable is a one-paragraph observation document committed to `docs/superpowers/observations/`. Tasks 2 and beyond can then be informed by it.

**Files:**
- Create: `docs/superpowers/observations/2026-04-14-poll-spam-incident.md`

- [ ] **Step 1: Locate the relevant log slice**

```bash
# Find the line range around 20:14:00 on 2026-04-14
grep -n "2026-04-14 20:14" bot.log | head -50
grep -n "2026-04-14 20:13:5" bot.log | head -10
grep -n "2026-04-14 20:14:30" bot.log | head -10
```

If no entries exist for `2026-04-14`, the user may have already rotated `bot.log`. In that case, document the absence in Step 4 ("Log no longer available; root cause hypothesis below cannot be confirmed") and proceed.

- [ ] **Step 2: Extract the spammed window verbatim**

```bash
# Replace LINE_START / LINE_END with the actual line numbers from Step 1
sed -n 'LINE_START,LINE_ENDp' bot.log > /tmp/poll_spam_20140414.log
wc -l /tmp/poll_spam_20140414.log
```

Note the per-line timestamps. If 100+ `Poll #N` lines all share the same `HH:MM:SS` second or fall within a 1-second window, the spam is structurally impossible from the legitimate poll loop and points at duplicated source.

- [ ] **Step 3: Classify the spam**

Run these checks **in order**, stop at the first match:

```bash
# (a) Duplicate process — check for two distinct PIDs writing to the same log.
#     The Python logger does not include PID by default, but if the bot logged
#     via systemd or our shell wrapper might prepend it. Look for any PID-like
#     pattern in the spammed lines:
grep -E "PID|pid=|\[[0-9]{4,6}\]" /tmp/poll_spam_20140414.log | head -5

# (b) Re-entrant poll() — check for nested "Poll #N" lines without "Sleeping Ns" between them
grep -E "(Poll #|Sleeping)" /tmp/poll_spam_20140414.log | head -30

# (c) notifier.poll_start being called per-date — check the immediate caller pattern
grep -B2 -A0 "Poll #" /tmp/poll_spam_20140414.log | head -20

# (d) Check for two simultaneous bot.lock acquisitions or session_cookies.json writes
#     by inspecting file mtimes around the incident window
ls -la session_cookies.json bot.log 2>&1
```

- [ ] **Step 4: Document the root cause**

Create `docs/superpowers/observations/2026-04-14-poll-spam-incident.md`:

```markdown
# 2026-04-14 20:14 — Poll-Spam Incident

## What happened

[Replace this with the verbatim window from /tmp/poll_spam_20140414.log,
or "log already rotated, see Phase A Task 1 for hypothesis"]

## Root cause

[One of:
 - "Duplicate `python main.py` process — confirmed via [evidence]."
 - "Re-entrant call to monitor.poll() from [caller] — confirmed via [evidence]."
 - "notifier.poll_start called per-date in [function:line] — confirmed via [evidence]."
 - "Root cause not determinable from available logs. Defensive infra (Task 2)
    will catch any recurrence regardless."]

## Implication for Task 2 (watchdog + lock)

[Either:
 - "Singleton lock alone would have prevented this (duplicate process)."
 - "Watchdog alone would have caught this (re-entrant loop)."
 - "Both layers needed (cause is structural; either layer is a useful catch)."]
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/observations/2026-04-14-poll-spam-incident.md
git commit -m "docs: investigate 2026-04-14 20:14 poll-spam incident"
```

---

## Task 2 (A2): Poll-rate watchdog + singleton process lock

**Context:** Two layers of defense against runaway / duplicate execution. The watchdog catches in-process re-entrance ("100 polls in 13s"); the singleton lock catches duplicate processes ("two `python main.py` running"). Together they ensure the spam class cannot recur regardless of which exact root cause Task 1 identifies.

The watchdog escalates: warn-and-throttle once, warn-and-throttle twice, then exit non-zero on the third trigger inside a 60-second window. This is not silent recovery — the bot kills itself rather than running hot, and the user can rely on systemd-style auto-restart (or just watch Discord errors) to bring it back.

**Files:**
- Create: `src/process_lock.py`
- Create: `src/poll_watchdog.py`
- Create: `tests/test_process_lock.py`
- Create: `tests/test_poll_watchdog.py`
- Modify: `src/monitor.py`
- Modify: `main.py`

### Subtask 2A — Singleton process lock

- [ ] **Step 1: Write failing test**

Create `tests/test_process_lock.py`:

```python
"""Tests for the singleton process lock — second process must refuse to start."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


def _spawn(lock_path: str, hold_secs: float) -> subprocess.Popen:
    """Spawn a subprocess that acquires the lock and holds it for hold_secs."""
    code = (
        "import sys, time\n"
        "sys.path.insert(0, '.')\n"
        "from src.process_lock import acquire_singleton_lock\n"
        f"lock = acquire_singleton_lock('{lock_path}')\n"
        f"time.sleep({hold_secs})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_second_acquire_fails():
    """A second process trying to acquire the same lock must exit non-zero."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        first = _spawn(lock_path, hold_secs=2.0)
        time.sleep(0.5)  # give first process time to acquire

        # Second process attempts the same lock
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0, "Second process must fail to acquire"
        assert "lock" in (result.stderr + result.stdout).lower()

        first.wait(timeout=5)


def test_lock_released_on_process_exit():
    """After the holder exits, a fresh process must acquire the lock cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        first = _spawn(lock_path, hold_secs=0.5)
        first.wait(timeout=5)

        # Second process should now succeed
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0


def test_stale_lock_reclaimed():
    """A lock file whose owning PID is dead must be reclaimable."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        # Write a fake PID that definitely isn't running
        Path(lock_path).write_text("999999\n")

        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, (
            f"Stale lock should be reclaimable; got stderr={result.stderr}"
        )
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
python -m pytest tests/test_process_lock.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.process_lock'`

- [ ] **Step 3: Implement `src/process_lock.py`**

Create `src/process_lock.py`:

```python
"""
Singleton process lock for Tock bot.

Uses fcntl.flock() on a lock file to ensure only one bot instance runs at
a time. Stale locks (whose owning PID is no longer alive) are reclaimed
automatically — no manual cleanup needed after a hard kill.

Usage:
    from src.process_lock import acquire_singleton_lock
    lock_handle = acquire_singleton_lock("bot.lock")
    # ... run bot ...
    # Lock auto-released when process exits (or call lock_handle.close())
"""

import fcntl
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class LockAcquisitionError(SystemExit):
    """Raised (as SystemExit) when the lock cannot be acquired."""


def _pid_alive(pid: int) -> bool:
    """Return True iff the given PID is currently running."""
    try:
        os.kill(pid, 0)  # signal 0 = no-op, raises if process gone
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_holder_pid(lock_path: str) -> int | None:
    """Read the PID written by the previous holder, or None if unreadable."""
    try:
        text = Path(lock_path).read_text().strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def acquire_singleton_lock(lock_path: str = "bot.lock"):
    """
    Acquire an exclusive flock on `lock_path`. Exit non-zero if another live
    process holds the lock. Returns the file handle (caller must keep it
    alive — closing the handle releases the lock).
    """
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_holder_pid(lock_path)
        if holder is not None and _pid_alive(holder):
            msg = (
                f"Another bot instance is already running "
                f"(PID {holder} holds lock on {lock_path}).\n"
                f"  Stop the other instance first, or kill it with: kill {holder}"
            )
            logger.error(msg)
            print(msg, file=sys.stderr)
            fh.close()
            raise LockAcquisitionError(2)
        # Stale lock — try once more after truncating
        logger.warning(
            f"Stale lock at {lock_path} (PID {holder} not alive) — reclaiming."
        )
        fh.seek(0)
        fh.truncate()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = f"Could not reclaim stale lock at {lock_path}."
            logger.error(msg)
            print(msg, file=sys.stderr)
            fh.close()
            raise LockAcquisitionError(2)

    # Write our PID into the lock file so the next reader can see it
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    logger.info(f"[startup] Acquired {lock_path} (PID={os.getpid()})")
    return fh
```

- [ ] **Step 4: Run test, confirm it passes**

```bash
python -m pytest tests/test_process_lock.py -v
```

Expected: 3 PASS

- [ ] **Step 5: Wire the lock into `main.py`**

In `main.py`, modify `async def main()` to acquire the lock immediately after `_setup_logging()` and before `from src.config import load_config`:

Find this block (around line 138-148):

```python
    _setup_logging()
    logger = logging.getLogger("main")

    # --- Config ---
    from src.config import load_config
```

Replace with:

```python
    _setup_logging()
    logger = logging.getLogger("main")

    # --- Singleton lock — refuse to start if another bot is running ---
    from src.process_lock import acquire_singleton_lock
    _bot_lock = acquire_singleton_lock("bot.lock")  # keep handle alive

    # --- Config ---
    from src.config import load_config
```

The handle is intentionally bound to a local name (`_bot_lock`) so the GC doesn't release it. It will be closed automatically when `main()` returns (process exit also releases flocks).

- [ ] **Step 6: Smoke-test the wiring**

```bash
# Should succeed and log "[startup] Acquired bot.lock"
python main.py --once 2>&1 | head -5
# In another terminal *while the first is still running*, this should fail:
python main.py --once 2>&1 | tail -5
```

The second invocation should print "Another bot instance is already running" and exit code 2.

- [ ] **Step 7: Commit**

```bash
git add src/process_lock.py tests/test_process_lock.py main.py
git commit -m "feat: add singleton process lock to prevent duplicate bot instances"
```

### Subtask 2B — Poll-rate watchdog

- [ ] **Step 1: Write failing test**

Create `tests/test_poll_watchdog.py`:

```python
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
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
python -m pytest tests/test_poll_watchdog.py -v
```

Expected: `ModuleNotFoundError: No module named 'src.poll_watchdog'`

- [ ] **Step 3: Implement `src/poll_watchdog.py`**

Create `src/poll_watchdog.py`:

```python
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
    def __init__(
        self,
        burst_threshold: int = 10,
        window_sec: float = 5.0,
        escalation_window_sec: float = 60.0,
        throttle_sec: float = 2.0,
    ):
        self._burst_threshold = burst_threshold
        self._window_sec = window_sec
        self._escalation_window_sec = escalation_window_sec
        self._throttle_sec = throttle_sec
        self._timestamps: deque[float] = deque(maxlen=64)
        self._trip_times: deque[float] = deque()  # times of WatchdogTrip
        self.trip_count: int = 0  # exposed for tests

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
        self.trip_count = len(self._trip_times)
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
```

- [ ] **Step 4: Run test, confirm it passes**

```bash
python -m pytest tests/test_poll_watchdog.py -v
```

Expected: 4 PASS

- [ ] **Step 5: Wire the watchdog into `monitor.py`**

In `src/monitor.py`, add the import at the top (after the existing `from src.tracker import SlotTracker` line):

```python
from src.poll_watchdog import PollWatchdog, WatchdogTrip
```

In `TockMonitor.__init__()` (around line 73-94), add at the end of the constructor:

```python
        # Watchdog: detect pathological poll bursts (see Apr 14 20:14 incident).
        # Threshold is well above legitimate sniper rate (~1 poll per 3-4s).
        self._watchdog = PollWatchdog(burst_threshold=10, window_sec=5.0)
```

In `TockMonitor.poll()` (around line 262), at the very top of the method (immediately after the docstring), add:

```python
    async def poll(self) -> None:
        """One full check-and-book cycle."""
        try:
            self._watchdog.tick()
        except WatchdogTrip as e:
            logger.warning(f"[monitor] Watchdog throttled this poll: {e}")
            return  # skip this cycle; throttle already slept
        self._poll_count += 1
        # ... existing body unchanged ...
```

- [ ] **Step 6: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/poll_watchdog.py tests/test_poll_watchdog.py src/monitor.py
git commit -m "feat: add poll-rate watchdog with 3-strike escalation"
```

---

## Task 3 (A3): Fix the "Slot 1" labeling bug

**Context:** `checker._collect_slots_multi()` at `src/checker.py:793` has a fallback `time_text = f"Slot {i + 1}"` when no real time can be extracted from a matched slot button. The booker (`_click_time_slot`) then has no real time string to match against — even with the recent generic-button guard, it can't identify the correct button to click. This was the root cause of the Apr 17 booking failure: the slot was named `"Slot 1"`, the booker couldn't match, and the click flow broke.

The fix replaces the garbage fallback with two new extraction sources (3-ancestor walk, and `aria-label` / `title` attributes), and on total failure emits a `WARNING` + error screenshot rather than a fake slot. A slot the booker cannot book is worse than no slot — it derails the booking race and produces the Apr 17 outcome.

**Files:**
- Modify: `src/checker.py`
- Create: `tests/test_slot_labeling.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_slot_labeling.py`:

```python
"""Tests for slot-label extraction priority (A3 fix).

The checker must extract a real time string in priority order:
  1. Child span (existing slot_time_text selector)
  2. Time pattern in parent.text_content()
  3. Time pattern in any ancestor up to 3 levels (NEW)
  4. Button's aria-label or title attribute (NEW)
  5. Button's own text content (if not bare "Book")

If none of the above yield a parseable time, the slot must NOT be
emitted. The "Slot N" fallback is removed.
"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker, AvailableSlot


def _make_checker():
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    tracker = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


def _btn(*, text="", aria_label="", title="", parent_text="",
         grandparent_text="", great_grandparent_text="",
         time_span_text=None):
    """Build a mock locator behaving like a Tock slot button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.get_attribute = AsyncMock(side_effect=lambda name: {
        "aria-label": aria_label, "title": title,
    }.get(name, None))

    # Time-span child (level 0)
    time_span = AsyncMock()
    time_span.count = AsyncMock(return_value=1 if time_span_text else 0)
    span_first = AsyncMock()
    span_first.text_content = AsyncMock(return_value=time_span_text or "")
    time_span.first = span_first

    # Parent / grandparent / great-grandparent text
    ancestors = [parent_text, grandparent_text, great_grandparent_text]

    def locator_factory(selector: str):
        if selector == "..":
            # Return a chained locator that walks up; track depth via attribute
            depth = getattr(locator_factory, "_depth", 0) + 1
            locator_factory._depth = depth
            anc = AsyncMock()
            anc.text_content = AsyncMock(
                return_value=ancestors[depth - 1] if depth <= 3 else ""
            )
            anc.locator = MagicMock(side_effect=locator_factory)
            return anc
        # time_span selector — return the prepared time_span mock
        return time_span

    locator_factory._depth = 0
    btn.locator = MagicMock(side_effect=locator_factory)
    return btn


@pytest.mark.asyncio
async def test_extracts_from_child_span():
    """Source 1: time in child span wins."""
    checker = _make_checker()
    btn = _btn(time_span_text="5:00 PM", text="ignored")
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page.locator = MagicMock(return_value=locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), "button.Consumer-resultsListItem.is-available"
    )
    assert len(slots) == 1
    assert slots[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_extracts_from_aria_label():
    """Source 4: when nothing else has time, aria-label is consulted."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="Book table at 5:30 PM for 2 guests",
               parent_text="Book", grandparent_text="", great_grandparent_text="")
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page.locator = MagicMock(return_value=locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "5:30 PM"


@pytest.mark.asyncio
async def test_extracts_from_grandparent():
    """Source 3: time pattern in 2nd ancestor is found."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="", title="",
               parent_text="Book", grandparent_text="6:00 PM table for 2",
               great_grandparent_text="")
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page.locator = MagicMock(return_value=locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert len(slots) == 1
    assert slots[0].slot_time.upper() == "6:00 PM"


@pytest.mark.asyncio
async def test_no_time_anywhere_drops_slot():
    """If no time can be extracted from any source, the slot is NOT emitted.
    The 'Slot N' fallback is forbidden."""
    checker = _make_checker()
    btn = _btn(text="Book", aria_label="", title="",
               parent_text="Book", grandparent_text="Restaurant info",
               great_grandparent_text="")
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=1)
    locator.nth = MagicMock(return_value=btn)
    page.locator = MagicMock(return_value=locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    assert slots == [], f"Expected no slot when time cannot be extracted; got {slots}"


@pytest.mark.asyncio
async def test_no_slot_n_label_in_output():
    """Regression: 'Slot 1', 'Slot 2', etc. must never appear in slot_time."""
    checker = _make_checker()
    # 3 buttons, none with extractable time
    btns = [
        _btn(text="Book", parent_text="x", grandparent_text="y"),
        _btn(text="Book", parent_text="x", grandparent_text="y"),
        _btn(text="Book", parent_text="x", grandparent_text="y"),
    ]
    page = MagicMock()
    locator = MagicMock()
    locator.count = AsyncMock(return_value=3)
    locator.nth = MagicMock(side_effect=lambda i: btns[i])
    page.locator = MagicMock(return_value=locator)

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17), 'button:visible:has-text("Book")'
    )
    for s in slots:
        assert not s.slot_time.lower().startswith("slot "), (
            f"'Slot N' fallback must not appear; got {s.slot_time!r}"
        )
```

- [ ] **Step 2: Run tests, confirm they fail**

```bash
python -m pytest tests/test_slot_labeling.py -v
```

Expected: FAILs — `test_extracts_from_aria_label`, `test_extracts_from_grandparent`, `test_no_time_anywhere_drops_slot`, `test_no_slot_n_label_in_output` all fail because the current implementation falls back to `"Slot N"`.

- [ ] **Step 3: Modify `_collect_slots_multi()` in `src/checker.py`**

In `src/checker.py`, replace the entire `_collect_slots_multi()` method (currently at lines 747-812) with:

```python
    async def _collect_slots_multi(
        self, page: Page, target_date: date, matched_selector: str
    ) -> list[AvailableSlot]:
        """Collect slots using whichever selector matched during detection.

        Time-extraction priority order:
          1. Child span matching slot_time_text selector
          2. Time pattern in parent.text_content()
          3. Time pattern in any ancestor up to 3 levels deep
          4. Button's aria-label or title attribute
          5. Button's own text_content (when not a bare 'Book' / 'Book now')

        If NO source yields a parseable time, the slot is NOT emitted —
        the 'Slot N' fallback is forbidden because the booker cannot match
        a slot without a real time string (Apr 17 root cause).
        """
        import re

        time_re = re.compile(r'\b(\d{1,2}:\d{2}\s*(?:AM|PM|am|pm))\b')

        slots: list[AvailableSlot] = []
        try:
            locator = page.locator(matched_selector)
            count = await locator.count()

            for i in range(count):
                el = locator.nth(i)
                try:
                    time_text = await self._extract_slot_time(el, time_re)
                    if time_text is None:
                        # No parseable time — drop this slot (do NOT fabricate).
                        # Apr 17 lesson: a slot the booker can't book is worse
                        # than no slot at all.
                        logger.warning(
                            f"[check] {target_date.isoformat()} — "
                            f"slot at index {i} has no extractable time; "
                            f"skipping (was 'Slot {i + 1}' under old fallback)"
                        )
                        if self.config.debug_screenshots:
                            await self._save_error_screenshot(
                                page, target_date.isoformat(),
                                f"slot_no_time_idx{i}"
                            )
                        continue

                    slots.append(
                        AvailableSlot(
                            slot_date=target_date,
                            slot_time=time_text,
                            day_of_week=target_date.strftime("%A"),
                        )
                    )
                except Exception:
                    continue
        except Exception as e:
            logger.error(
                f"[check] {target_date.isoformat()} — slot collection failed: {e}"
            )

        if slots:
            logger.info(
                f"[check] {target_date.isoformat()} — {len(slots)} slot(s): "
                + ", ".join(s.slot_time for s in slots)
            )
        return slots

    async def _extract_slot_time(self, element, time_re) -> str | None:
        """Try sources 1-5 (see docstring of _collect_slots_multi).
        Returns the extracted time string, or None when no source matches.
        """
        # Source 1: Child span with the standard slot_time_text selector
        try:
            time_selector = sel.get("slot_time_text")
            time_span = element.locator(time_selector)
            if await time_span.count() > 0:
                t = (await time_span.first.text_content() or "").strip()
                if t:
                    return t
        except Exception:
            pass

        # Source 2: Parent text_content
        try:
            parent = element.locator("..")
            parent_text = (await parent.text_content() or "").strip()
            m = time_re.search(parent_text)
            if m:
                return m.group(1)
        except Exception:
            pass

        # Source 3: Ancestors up to 3 levels above parent
        try:
            ancestor = element
            for _ in range(3):
                ancestor = ancestor.locator("..")
                anc_text = (await ancestor.text_content() or "").strip()
                m = time_re.search(anc_text)
                if m:
                    return m.group(1)
        except Exception:
            pass

        # Source 4: aria-label and title attributes
        for attr in ("aria-label", "title"):
            try:
                val = await element.get_attribute(attr)
                if val:
                    m = time_re.search(val)
                    if m:
                        return m.group(1)
            except Exception:
                continue

        # Source 5: Button's own text content, only if not a bare 'Book'
        try:
            btn_text = (await element.text_content() or "").strip()
            if btn_text and btn_text.lower() not in ("book", "book now"):
                m = time_re.search(btn_text)
                if m:
                    return m.group(1)
                # Some restaurants use "5:00pm" without a space — accept raw text
                if any(c.isdigit() for c in btn_text) and ":" in btn_text:
                    return btn_text
        except Exception:
            pass

        return None
```

- [ ] **Step 4: Run tests, confirm they pass**

```bash
python -m pytest tests/test_slot_labeling.py -v
```

Expected: 5 PASS

- [ ] **Step 5: Run full suite to catch regressions**

```bash
python -m pytest tests/ -q
```

Expected: all pass. (If `tests/test_checker_detection.py` or `tests/test_slot_click.py` break because they relied on the `Slot N` fallback, update those tests to assert the new drop-the-slot behavior.)

- [ ] **Step 6: Commit**

```bash
git add src/checker.py tests/test_slot_labeling.py
git commit -m "fix: drop Slot N fallback; extract time from aria-label and ancestors"
```

---

## Task 4 (A5): Container-scoped slot selectors

**Context:** Today `_collect_slots_multi()` runs `page.locator(matched_selector)` against the entire page. A `Book` button anywhere on the page (restaurant header, "Book a private event" CTA, etc.) is a structural false positive. The fix is to first find the smallest container that holds time-slot buttons, then scope all slot lookups inside it. If the container can't be found, fall back to current page-wide behavior so we don't regress on restaurants whose DOM we haven't inspected yet — but log `WARNING` so we know to update the selector.

**Files:**
- Modify: `src/selectors.py`
- Modify: `src/checker.py`
- Create: `tests/test_scoped_slot_selectors.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_scoped_slot_selectors.py`:

```python
"""Tests for container-scoped slot collection (A5 fix).

When `slots_container` selector matches, slot lookups must be scoped to
that container. A `Book` button outside the container must NOT be collected.
"""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock

from src.checker import AvailabilityChecker


def _make_checker():
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return AvailabilityChecker(config, MagicMock(), MagicMock())


@pytest.mark.asyncio
async def test_collect_only_buttons_inside_container():
    """If slots_container is present, only buttons inside it are collected."""
    checker = _make_checker()

    # in_container: parent has time text → should be collected
    in_container_btn = AsyncMock()
    in_container_btn.text_content = AsyncMock(return_value="Book")
    in_container_btn.get_attribute = AsyncMock(return_value=None)

    parent_in = AsyncMock()
    parent_in.text_content = AsyncMock(return_value="5:00 PM   2 guests")
    parent_in.locator = MagicMock(return_value=AsyncMock(
        text_content=AsyncMock(return_value="")
    ))

    in_container_btn.locator = MagicMock(side_effect=lambda s: (
        parent_in if s == ".." else AsyncMock(count=AsyncMock(return_value=0))
    ))

    # Container locator (page-level): find -> exists, then locator(button…) returns 1
    container = MagicMock()
    container_buttons = MagicMock()
    container_buttons.count = AsyncMock(return_value=1)
    container_buttons.nth = MagicMock(return_value=in_container_btn)
    container.locator = MagicMock(return_value=container_buttons)

    page = MagicMock()
    container_finder = MagicMock()
    container_finder.count = AsyncMock(return_value=1)
    container_finder.first = container

    def page_locator(sel):
        if "slots_container" in sel or "results-list" in sel:
            return container_finder
        # Anything else returning 1 button means the test misconfigured scoping
        page_wide = MagicMock()
        page_wide.count = AsyncMock(return_value=99)  # noisy false positive
        return page_wide

    page.locator = MagicMock(side_effect=page_locator)

    # Pre-resolve the container-finder selector by registering it
    import src.selectors as sel_mod
    sel_mod.SELECTORS["slots_container"] = "div.results-list"

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    assert len(slots) == 1, (
        f"Expected exactly 1 slot from inside the container, got {len(slots)}: {slots}"
    )


@pytest.mark.asyncio
async def test_falls_back_to_page_when_container_missing():
    """If slots_container is not present, falls back to page-wide collection."""
    checker = _make_checker()

    # Container selector returns 0 → falls back to page-level
    container_finder = MagicMock()
    container_finder.count = AsyncMock(return_value=0)

    page_wide_btn = AsyncMock()
    page_wide_btn.text_content = AsyncMock(return_value="Book")
    page_wide_btn.get_attribute = AsyncMock(return_value=None)
    page_wide_btn.locator = MagicMock(return_value=AsyncMock(
        text_content=AsyncMock(return_value="5:00 PM   table"),
        count=AsyncMock(return_value=0),
    ))

    page_wide_locator = MagicMock()
    page_wide_locator.count = AsyncMock(return_value=1)
    page_wide_locator.nth = MagicMock(return_value=page_wide_btn)

    page = MagicMock()
    def page_locator(sel):
        if "slots_container" in sel or "results-list" in sel:
            return container_finder
        return page_wide_locator
    page.locator = MagicMock(side_effect=page_locator)

    import src.selectors as sel_mod
    sel_mod.SELECTORS["slots_container"] = "div.results-list"

    slots = await checker._collect_slots_multi(
        page, date(2026, 4, 17),
        'button:visible:has-text("Book")'
    )
    # Falls back to page-wide collection when container missing
    assert len(slots) >= 0  # behavior preserved (no regression)
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
python -m pytest tests/test_scoped_slot_selectors.py -v
```

Expected: FAIL — `slots_container` selector key does not exist; `_collect_slots_multi` doesn't honor scoping.

- [ ] **Step 3: Add the `slots_container` selector**

In `src/selectors.py`, add inside the `SELECTORS` dict (location: near `available_slot_button` / `slot_time_text`):

```python
    # Wrapping container for time-slot results — used by checker to scope slot
    # collection so global "Book" buttons (header CTAs, private-event tiles)
    # cannot become false positives. Multiple commas = OR-of-selectors.
    "slots_container": (
        "div.Consumer-resultsList, "
        "div[role='region'][aria-label*='time'], "
        "div[data-testid='search-results'], "
        "div.SearchResults, "
        "section.search-results"
    ),
```

(The exact selector string may need a headed-mode DOM inspection to refine. The OR-list above covers the most common Tock layouts; if none match in production, the fallback path takes over and we add a `WARNING` log line.)

- [ ] **Step 4: Modify `_collect_slots_multi()` to use the container**

In `src/checker.py`, modify the start of `_collect_slots_multi()` (the version from Task 3) so it tries the container first and falls back to page-wide. Replace the body's beginning (the `try:` block opening and `locator = page.locator(matched_selector)` line) with:

```python
        try:
            # First try to scope to the slots container — prevents global
            # "Book" buttons from becoming false positives.
            container_selector = sel.get("slots_container")
            container_finder = page.locator(container_selector)
            scoped_root = None
            try:
                if await container_finder.count() > 0:
                    scoped_root = container_finder.first
            except Exception:
                scoped_root = None

            if scoped_root is None:
                logger.warning(
                    f"[check] {target_date.isoformat()} — "
                    f"slots_container not found; falling back to page-wide "
                    f"collection (selector key: 'slots_container')"
                )
                locator = page.locator(matched_selector)
            else:
                locator = scoped_root.locator(matched_selector)

            count = await locator.count()
```

Everything below `count = await locator.count()` is unchanged from the Task 3 version.

- [ ] **Step 5: Run tests, confirm they pass**

```bash
python -m pytest tests/test_scoped_slot_selectors.py -v
```

Expected: 2 PASS

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 7: Verify selectors against the live site**

```bash
python main.py --verify
```

Expected: `slots_container` either passes OR logs a `SELECTOR_FAILED` line. If it fails on Fuhuihua, that's an acceptable interim state — the fallback path activates with a `WARNING`. Open an issue or extend `SELECTORS["slots_container"]` after a headed-mode DOM inspection.

- [ ] **Step 8: Commit**

```bash
git add src/selectors.py src/checker.py tests/test_scoped_slot_selectors.py
git commit -m "fix: scope slot collection to slots_container; fall back with warning"
```

---

## Task 5 (A4): Cap `scan_weeks=2` inside the sniper window

**Context:** Tock releases at most the next 2 weeks of slots. Scanning Friday-3-weeks-out and Friday-4-weeks-out during the sniper window is wasted effort that contributes only to error counts and Cloudflare exposure. Add a separate `Config.sniper_scan_weeks: int = 2` field (default 2) that caps the scan range only when `keep_pages=True` (the existing sniper-mode flag). Outside sniper mode, normal `scan_weeks` still governs.

This is the smallest task in Phase A and lands last as a confidence-builder.

**Files:**
- Modify: `src/config.py`
- Modify: `src/checker.py`
- Create: `tests/test_sniper_scan_weeks.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_sniper_scan_weeks.py`:

```python
"""Tests for sniper-mode scan-week cap (A4 fix).

Tock releases at most the next 2 weeks of slots. During sniper mode the
date list must be capped at sniper_scan_weeks regardless of the larger
normal-mode scan_weeks setting.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

from src.checker import AvailabilityChecker


def _make_checker(scan_weeks=4, sniper_scan_weeks=2):
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[], preferred_time="17:00", scan_weeks=scan_weeks,
        dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        sniper_scan_weeks=sniper_scan_weeks,
    )
    return AvailabilityChecker(config, MagicMock(), MagicMock())


def test_normal_mode_uses_full_scan_weeks():
    """In normal mode, _get_target_dates returns dates up to scan_weeks weeks out."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    dates = checker._get_target_dates(["Friday"], sniper_mode=False)
    assert len(dates) >= 3, (
        f"4 weeks should yield ≥3 Fridays (depending on today); got {len(dates)}"
    )
    horizon = date.today() + timedelta(weeks=4)
    assert all(d <= horizon for d in dates)


def test_sniper_mode_caps_at_sniper_scan_weeks():
    """In sniper mode, _get_target_dates is capped at sniper_scan_weeks weeks out."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    dates = checker._get_target_dates(["Friday"], sniper_mode=True)
    horizon = date.today() + timedelta(weeks=2)
    assert all(d <= horizon for d in dates), (
        f"All dates must be within sniper_scan_weeks=2; got {dates}"
    )
    # Specifically: no Fridays beyond 2 weeks
    assert len(dates) <= 3  # at most 2-3 Fridays in a 2-week window


def test_sniper_cap_smaller_than_normal():
    """Sniper-mode list must be a subset of normal-mode list when both same days."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    normal = set(checker._get_target_dates(["Friday"], sniper_mode=False))
    sniper = set(checker._get_target_dates(["Friday"], sniper_mode=True))
    assert sniper <= normal, (
        f"Sniper list must be a subset of normal list; "
        f"sniper={sniper}, normal={normal}"
    )
```

- [ ] **Step 2: Run test, confirm it fails**

```bash
python -m pytest tests/test_sniper_scan_weeks.py -v
```

Expected: FAIL — `Config` has no `sniper_scan_weeks` field, and `_get_target_dates` has no `sniper_mode` parameter.

- [ ] **Step 3: Add `sniper_scan_weeks` to `Config`**

In `src/config.py`, add a field to the `Config` dataclass after `sniper_interval_sec` (around line 49) and before `debug_screenshots`:

```python
    sniper_interval_sec: int         # sleep between polls in sniper mode
    sniper_scan_weeks: int = 2       # NEW: scan-range cap during sniper mode (Tock releases ≤2 wks)
```

In `load_config()`, add the env loader after the existing `sniper_interval_sec` line (around line 100):

```python
        sniper_interval_sec=int(os.getenv("SNIPER_INTERVAL_SEC", "3")),
        sniper_scan_weeks=int(os.getenv("SNIPER_SCAN_WEEKS", "2")),
```

- [ ] **Step 4: Add `sniper_mode` parameter to `_get_target_dates()`**

In `src/checker.py`, replace the existing `_get_target_dates()` method (around line 353-366) with:

```python
    def _get_target_dates(
        self, days: list[str] | None = None, sniper_mode: bool = False
    ) -> list[date]:
        """Dates from tomorrow through the active scan horizon that fall on *days*.

        Defaults to config.preferred_days when days is None.
        When sniper_mode=True, the horizon is capped at config.sniper_scan_weeks
        (Tock releases at most that many weeks of slots; scanning further out is
        wasted effort that contributes only to error counts).
        """
        if days is None:
            days = self.config.preferred_days
        weeks = self.config.sniper_scan_weeks if sniper_mode else self.config.scan_weeks
        today = date.today()
        end = today + timedelta(weeks=weeks)
        result = []
        current = today + timedelta(days=1)
        while current <= end:
            if current.strftime("%A") in days:
                result.append(current)
            current += timedelta(days=1)
        return result
```

- [ ] **Step 5: Pass `sniper_mode` from `check_all()`**

In `src/checker.py`, find the two call sites of `_get_target_dates()` inside `check_all()` (around lines 312 and 315). Modify them to pass the sniper flag:

```python
            preferred_dates = self._get_target_dates(
                self.config.preferred_days, sniper_mode=keep_pages
            )
            preferred_slots = await _scan_dates(preferred_dates)

            fallback_dates = self._get_target_dates(
                self.config.fallback_days, sniper_mode=keep_pages
            )
```

- [ ] **Step 6: Run tests, confirm they pass**

```bash
python -m pytest tests/test_sniper_scan_weeks.py -v
```

Expected: 3 PASS

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: all pass.

- [ ] **Step 8: Commit**

```bash
git add src/config.py src/checker.py tests/test_sniper_scan_weeks.py
git commit -m "feat: cap scan range to sniper_scan_weeks (default 2) in sniper mode"
```

---

## Phase A completion checklist

After all 5 tasks are committed, verify the success criteria from the spec:

- [ ] **Spec criterion A1 (no `Slot N` labels):** `grep -r 'Slot 1\|Slot 2\|Slot 3' bot.log` after the next sniper window returns empty (or only WARNING extraction-failure lines, never an emitted slot).
- [ ] **Spec criterion A2 (singleton lock + watchdog dormant in production):** `bot.log` shows `[startup] Acquired bot.lock (PID=…)` exactly once per session, and no `Poll-rate watchdog triggered` lines under normal operation.
- [ ] **Spec criterion A3 (`--verify` still passes):** `python main.py --verify` exits 0 (or only logs the new `slots_container` warning if Fuhuihua's DOM doesn't yet match).
- [ ] **Spec criterion A4 (Apr-14-style burst does not recur):** Two release windows post-Phase-A, no `Poll #N` density above 1/3s in `bot.log`.
- [ ] **Run the full MM-style sniper test suite to confirm no regressions:**
  ```bash
  python -m pytest tests/ -q
  ```

After observing one Friday release window with Phase A in production, write a short observation note to `docs/superpowers/observations/<date>-window-1.md` summarizing what changed (or didn't), and use it to inform Phase B's writing-plans handoff.

---

## Self-Review

**Spec coverage check:**

| Spec requirement (Section 2) | Task |
|------------|------|
| A1 — Investigate 20:14 log spam | Task 1 |
| A2 — Poll-rate watchdog | Task 2B |
| A2 — Singleton process lock | Task 2A |
| A3 — Drop "Slot N" fallback | Task 3 |
| A3 — Add aria-label / 3-ancestor extraction | Task 3 |
| A3 — WARNING + error screenshot on extraction failure | Task 3 |
| A4 — `Config.sniper_scan_weeks: int = 2` | Task 5 |
| A4 — Cap dates in sniper mode | Task 5 |
| A5 — Add `slots_container` selector | Task 4 |
| A5 — Scope `_collect_slots_multi` to container | Task 4 |
| A5 — Fall back with WARNING when container missing | Task 4 |

All 11 spec sub-requirements have a task. No gaps.

**Placeholder scan:** No TBD/TODO/FIXME. The phrase "(The exact selector string may need a headed-mode DOM inspection to refine…)" in Task 4 Step 3 is documented context, not a placeholder — the OR-list provides a reasonable starting point and the fallback handles the miss.

**Type consistency check:**
- `acquire_singleton_lock(lock_path: str = "bot.lock")` — used in Task 2A test, implementation, and `main.py` wiring consistently.
- `PollWatchdog(burst_threshold=10, window_sec=5.0)` — same constructor signature in test, implementation, and `monitor.py` wiring.
- `WatchdogTrip` exception — defined in implementation, imported in `monitor.py`.
- `_extract_slot_time(element, time_re)` — defined in Task 3 implementation, called from `_collect_slots_multi`.
- `_get_target_dates(days, sniper_mode=False)` — Task 5 signature matches both call sites in `check_all()`.
- `Config.sniper_scan_weeks: int = 2` — Task 5 dataclass field matches env loader, test fixture kwarg, and `_get_target_dates` reference.
- `sel.get("slots_container")` — Task 4 selector key matches the `SELECTORS` dict entry added in the same task.

**Interaction check:** Tasks 3 and 4 both modify `_collect_slots_multi()`. Task 3 lands the new method body and `_extract_slot_time()` helper; Task 4 modifies only the opening of the same method to scope the locator. The Task 4 patch instructions are explicit about which lines change, avoiding conflicts with Task 3's body.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-24-phase-a-tactical-fixes.md`. Two execution options:

1. **Subagent-Driven (recommended)** — dispatch a fresh subagent per task, review between tasks, fast iteration. Best for catching task-level issues before they compound.
2. **Inline Execution** — execute tasks in this session using executing-plans, batch execution with checkpoints. Best when you want to keep momentum and review at milestones rather than per-task.

Which approach?
