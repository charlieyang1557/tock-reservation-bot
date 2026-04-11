# Sniper Critical Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the four failure modes observed on April 10 (pre-release errors causing sequential degradation, full-date-scan delay before booking, wrong button click, checkout not found) and add two supporting features (booking screenshots, suppress Discord before booking).

**Architecture:** Seven independent-ish tasks in two parallel tracks: Track A (Tasks 1–3) tightens sniper scheduling logic in `monitor.py` and `checker.py`; Track B (Tasks 4–6) fixes the booking flow in `booker.py` and `notifier.py`. Task 7 adds the `--test-sniper-phases` integration test. All tasks follow TDD: failing test → implementation → passing test → commit.

**Tech Stack:** Python 3.11, asyncio, Playwright async_api, pytest, pytest-asyncio

---

## File Structure

| File | Changes |
|------|---------|
| `src/monitor.py` | Extract `_apply_adaptive_switching(sniper_age)` method; pass to poll() |
| `src/checker.py` | Add pre-release early-return in `check_all()`; add `abort_event` to `_check_date()` |
| `src/booker.py` | Guard generic-button clicks; scroll-to-bottom after slot click; polling checkout wait; screenshot helper |
| `src/notifier.py` | Add `sniper_mode=False` param to `slots_found()` |
| `main.py` | Add `--test-sniper-phases` flag |
| `src/testing/sniper_tests.py` | Add `test_sniper_phases()` function |
| `tests/test_sniper_phases.py` | New test file (Tasks 1, 2, 6) |
| `tests/test_interrupt_scan.py` | New test file (Task 3) |
| `tests/test_booking_fixes.py` | New test file (Tasks 4, 5) |

---

## Task 1: Gate adaptive degradation on release time

**Context:** `TockMonitor.poll()` (lines ~299–333 in `src/monitor.py`) updates `_sniper_error_window` whenever `_sniper_active` and `last_checks > 0`. On April 10, every calendar page at 19:29 returned `calendar_container` timeout (pre-release state = all slots sold). That 100% error rate immediately forced `_sniper_concurrent = False`, slowing the bot to sequential mode before slots even existed. Fix: extract the adaptive block into `_apply_adaptive_switching(sniper_age)` and skip it when `sniper_age < 60.0`.

**Files:**
- Modify: `src/monitor.py`
- Create: `tests/test_sniper_phases.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_sniper_phases.py`:

```python
"""Tests for sniper phase logic: pre-release error gating and two-phase scan."""
import pytest
from unittest.mock import MagicMock


def _make_monitor():
    """Minimal TockMonitor wired with mock dependencies."""
    from src.config import Config
    from src.monitor import TockMonitor

    config = Config(
        tock_email="test@test.com",
        tock_password="pw",
        restaurant_slug="test-slug",
        party_size=2,
        preferred_days=["Friday"],
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=4,
        dry_run=True,
        headless=True,
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="",
        card_cvc="",
    )
    browser = MagicMock()
    checker = MagicMock()
    checker.last_errors = 6
    checker.last_checks = 6
    notifier = MagicMock()
    tracker = MagicMock()
    monitor = TockMonitor(config, browser, checker, notifier, tracker)
    monitor._sniper_active = True
    monitor._sniper_concurrent = True
    return monitor


def test_no_degradation_before_release():
    """100% errors at sniper_age=30s must NOT change concurrent mode."""
    monitor = _make_monitor()
    monitor._apply_adaptive_switching(sniper_age=30.0)
    assert monitor._sniper_concurrent is True


def test_degradation_after_release():
    """100% errors at sniper_age=90s MUST degrade to sequential mode."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0  # any error triggers switch
    monitor._apply_adaptive_switching(sniper_age=90.0)
    assert monitor._sniper_concurrent is False


def test_boundary_exactly_60s():
    """sniper_age=60.0 is post-release — errors should count."""
    monitor = _make_monitor()
    monitor._SNIPER_ERROR_THRESH = 0.0
    monitor._apply_adaptive_switching(sniper_age=60.0)
    assert monitor._sniper_concurrent is False


def test_recovery_still_works_post_release():
    """After degradation, 3 clean polls restore concurrent mode."""
    monitor = _make_monitor()
    monitor._sniper_concurrent = False
    monitor._sniper_sequential_clean = 0
    monitor.checker.last_errors = 0
    monitor.checker.last_checks = 6
    for _ in range(3):
        monitor._apply_adaptive_switching(sniper_age=120.0)
    assert monitor._sniper_concurrent is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_sniper_phases.py -v
```

Expected: `AttributeError: 'TockMonitor' object has no attribute '_apply_adaptive_switching'`

- [ ] **Step 3: Extract `_apply_adaptive_switching` in `monitor.py`**

In `src/monitor.py`, locate the adaptive block in `poll()` (currently lines ~299–333). Replace it with a call to a new method.

Replace this block in `poll()`:
```python
        # --- Adaptive sniper mode switching ---
        if self._sniper_active and self.checker.last_checks > 0:
            rate = self.checker.last_errors / self.checker.last_checks
            self._sniper_error_window.append(rate)
            if len(self._sniper_error_window) > self._SNIPER_WINDOW_SIZE:
                self._sniper_error_window.pop(0)
            rolling_rate = sum(self._sniper_error_window) / len(self._sniper_error_window)

            if self._sniper_concurrent and rolling_rate > self._SNIPER_ERROR_THRESH:
                self._sniper_concurrent = False
                self._sniper_sequential_clean = 0
                logger.warning(
                    f"[sniper] Concurrent error rate {rolling_rate:.0%} > "
                    f"{self._SNIPER_ERROR_THRESH:.0%} threshold — "
                    f"switching to SEQUENTIAL mode"
                )
            elif not self._sniper_concurrent:
                if rate == 0.0:
                    self._sniper_sequential_clean += 1
                else:
                    self._sniper_sequential_clean = 0
                if self._sniper_sequential_clean >= self._SNIPER_RECOVER_POLLS:
                    self._sniper_concurrent = True
                    self._sniper_error_window.clear()
                    self._sniper_sequential_clean = 0
                    logger.info(
                        f"[sniper] {self._SNIPER_RECOVER_POLLS} clean sequential polls "
                        f"— switching back to CONCURRENT mode"
                    )
            else:
                logger.debug(
                    f"[sniper] {'concurrent' if self._sniper_concurrent else 'sequential'} "
                    f"error rate this poll: {rate:.0%} "
                    f"(rolling {rolling_rate:.0%})"
                )
```

With:
```python
        # --- Adaptive sniper mode switching ---
        self._apply_adaptive_switching(sniper_age)
```

Add the new method to `TockMonitor`, after the `run()` method and before `_get_poll_interval()`:

```python
    def _apply_adaptive_switching(self, sniper_age: float) -> None:
        """Update concurrent/sequential mode based on rolling error rate.

        Only applies AFTER the release time has passed (sniper_age >= 60s).
        The sniper window starts 1 minute before release; errors in the first
        60s are expected (sold-out state) and must not count toward degradation.
        """
        if not self._sniper_active:
            return
        if self.checker.last_checks <= 0:
            return
        if sniper_age < 60.0:
            logger.debug(
                f"[sniper] Pre-release (age={sniper_age:.1f}s) — "
                "ignoring errors for adaptive switching"
            )
            return

        rate = self.checker.last_errors / self.checker.last_checks
        self._sniper_error_window.append(rate)
        if len(self._sniper_error_window) > self._SNIPER_WINDOW_SIZE:
            self._sniper_error_window.pop(0)
        rolling_rate = sum(self._sniper_error_window) / len(self._sniper_error_window)

        if self._sniper_concurrent and rolling_rate > self._SNIPER_ERROR_THRESH:
            self._sniper_concurrent = False
            self._sniper_sequential_clean = 0
            logger.warning(
                f"[sniper] Concurrent error rate {rolling_rate:.0%} > "
                f"{self._SNIPER_ERROR_THRESH:.0%} threshold — "
                f"switching to SEQUENTIAL mode"
            )
        elif not self._sniper_concurrent:
            if rate == 0.0:
                self._sniper_sequential_clean += 1
            else:
                self._sniper_sequential_clean = 0
            if self._sniper_sequential_clean >= self._SNIPER_RECOVER_POLLS:
                self._sniper_concurrent = True
                self._sniper_error_window.clear()
                self._sniper_sequential_clean = 0
                logger.info(
                    f"[sniper] {self._SNIPER_RECOVER_POLLS} clean sequential polls "
                    f"— switching back to CONCURRENT mode"
                )
        else:
            logger.debug(
                f"[sniper] {'concurrent' if self._sniper_concurrent else 'sequential'} "
                f"error rate this poll: {rate:.0%} "
                f"(rolling {rolling_rate:.0%})"
            )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_sniper_phases.py -v
```

Expected: 4 PASS

- [ ] **Step 5: Run full suite to catch regressions**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/monitor.py tests/test_sniper_phases.py
git commit -m "fix: gate adaptive degradation on sniper_age >= 60s (post-release only)"
```

---

## Task 2: Two-phase sniper — skip calendar before release

**Context:** `AvailabilityChecker.check_all()` in `src/checker.py` receives `sniper_window_age_sec` from `monitor.poll()`. When `sniper_window_age_sec < 60` the actual release hasn't happened yet. Scanning calendars before release produces only timeouts (nothing to find), burning session resources and contributing to the error rate that Task 1 now ignores. Fix: at the top of `check_all()`, if `keep_pages and sniper_window_age_sec < 60`, return `[]` immediately without touching any calendar page. Phase 2 (aggressive scan) kicks in automatically at 60s.

**Files:**
- Modify: `src/checker.py`
- Modify: `tests/test_sniper_phases.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_sniper_phases.py`:

```python
import pytest
from unittest.mock import AsyncMock, patch


def _make_checker():
    from src.checker import AvailabilityChecker
    from src.config import Config

    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record_deferred = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


@pytest.mark.asyncio
async def test_pre_release_skips_calendar_scan():
    """check_all with sniper_age < 60s returns [] without calling _check_date."""
    checker = _make_checker()
    with patch.object(checker, '_check_date', new_callable=AsyncMock) as mock_check:
        result = await checker.check_all(
            concurrent=True,
            keep_pages=True,
            sniper_window_age_sec=30.0,
        )
    assert result == []
    mock_check.assert_not_called()


@pytest.mark.asyncio
async def test_pre_release_resets_error_counters():
    """Pre-release return clears last_errors and last_checks (no phantom errors)."""
    checker = _make_checker()
    checker.last_errors = 99
    checker.last_checks = 99
    with patch.object(checker, '_check_date', new_callable=AsyncMock):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=10.0
        )
    assert checker.last_errors == 0
    assert checker.last_checks == 0


@pytest.mark.asyncio
async def test_post_release_proceeds_to_scan():
    """check_all with sniper_age >= 60s calls _check_date (normal aggressive mode)."""
    checker = _make_checker()
    with patch.object(checker, '_check_date', new_callable=AsyncMock, return_value=[]) as mock_check:
        await checker.check_all(
            concurrent=True,
            keep_pages=True,
            sniper_window_age_sec=61.0,
        )
    assert mock_check.call_count > 0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_sniper_phases.py::test_pre_release_skips_calendar_scan -v
```

Expected: FAIL (`_check_date` is called when it shouldn't be)

- [ ] **Step 3: Add pre-release early-return in `check_all()`**

In `src/checker.py`, inside `check_all()`, after the line:
```python
        self._skip_cache_enabled = keep_pages and sniper_window_age_sec > 300
        # Always clear stale entries at poll start so we retry dates that
        # failed last poll
        self._skip_dates.clear()
```

Add:

```python
        # ── Two-phase sniper: Phase 1 (pre-release) ──────────────────────────
        # The sniper window starts 60s before the actual release time.
        # Scanning calendars before release produces only timeouts and error
        # counts. Return immediately; Phase 2 (aggressive scan) begins at 60s.
        if keep_pages and sniper_window_age_sec < 60.0:
            self.last_errors = 0
            self.last_checks = 0
            logger.debug(
                f"[check] Pre-release phase (age={sniper_window_age_sec:.1f}s) — "
                "skipping calendar scan until release"
            )
            return []
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
python -m pytest tests/test_sniper_phases.py -v
```

Expected: All 7 PASS (4 from Task 1 + 3 new).

- [ ] **Step 5: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 6: Commit**

```bash
git add src/checker.py tests/test_sniper_phases.py
git commit -m "fix: skip calendar scan during pre-release phase (sniper_age < 60s)"
```

---

## Task 3: Interrupt scan on first slot detection

**Context:** `AvailabilityChecker.check_all()` uses `asyncio.gather()` across all preferred dates. Even after one date finds slots at T+5s, the remaining 5 dates complete their full scan (~34s total). The monitor and booker wait for `check_all()` to return before booking starts. Fix: pass an `asyncio.Event abort_event` into each concurrent `_check_date()` call. The first date to find slots sets the event; all other tasks check it at natural `await` points and return `[]` early. `asyncio.gather()` then resolves within ~500ms of first detection instead of waiting for all dates.

**Files:**
- Modify: `src/checker.py`
- Create: `tests/test_interrupt_scan.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_interrupt_scan.py`:

```python
"""Tests for first-slot interrupt during concurrent sniper scanning."""
import asyncio
import pytest
from datetime import date
from unittest.mock import MagicMock, AsyncMock, patch

from src.checker import AvailabilityChecker, AvailableSlot


def _make_checker():
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[], preferred_time="17:00", scan_weeks=4,
        dry_run=True, headless=True, sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", debug_screenshots=False,
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record_deferred = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(config, browser, tracker)


@pytest.mark.asyncio
async def test_abort_event_passed_to_check_date():
    """In sniper concurrent mode, _check_date receives a non-None abort_event."""
    checker = _make_checker()
    received_events = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        received_events.append(abort_event)
        return []

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=90.0
        )

    assert all(e is not None for e in received_events), (
        "abort_event must be non-None in sniper concurrent mode"
    )


@pytest.mark.asyncio
async def test_remaining_tasks_aborted_after_first_slot():
    """After one date finds a slot, remaining concurrent tasks see event set and return []."""
    checker = _make_checker()

    dates_checked = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        dates_checked.append(target_date.isoformat())
        # If abort event already set, return immediately (simulates fast abort)
        if abort_event and abort_event.is_set():
            return []
        # First date: return a slot and set the event
        slot = AvailableSlot(
            slot_date=target_date,
            slot_time="5:00 PM",
            day_of_week=target_date.strftime("%A"),
        )
        if abort_event:
            abort_event.set()
        return [slot]

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=90.0
        )

    assert len(result) >= 1
    assert result[0].slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_sequential_sniper_stops_after_first_slot():
    """In sequential sniper mode, scanning stops after first date with slots."""
    checker = _make_checker()

    dates_scanned = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        dates_scanned.append(target_date.isoformat())
        return [AvailableSlot(
            slot_date=target_date, slot_time="5:00 PM",
            day_of_week=target_date.strftime("%A"),
        )]

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        result = await checker.check_all(
            concurrent=False, keep_pages=True, sniper_window_age_sec=90.0
        )

    # Should stop after the first date finds slots
    assert len(dates_scanned) == 1
    assert len(result) == 1


@pytest.mark.asyncio
async def test_non_sniper_concurrent_no_abort_event():
    """Non-sniper concurrent scans must NOT receive an abort_event (full scan)."""
    checker = _make_checker()
    received_events = []

    async def fake_check_date(target_date, keep_page=False, abort_event=None):
        received_events.append(abort_event)
        return []

    with patch.object(checker, '_check_date', side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=False, sniper_window_age_sec=0.0
        )

    assert all(e is None for e in received_events), (
        "abort_event must be None in non-sniper mode (full scan required)"
    )
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_interrupt_scan.py -v
```

Expected: FAIL (`_check_date` doesn't accept `abort_event` parameter yet)

- [ ] **Step 3: Add `abort_event` parameter to `_check_date()`**

In `src/checker.py`, change the signature of `_check_date()`:

```python
    async def _check_date(
        self, target_date: date, keep_page: bool = False,
        abort_event: "asyncio.Event | None" = None,
    ) -> list[AvailableSlot]:
```

Add `import asyncio` at the top of the file (after the existing imports):

```python
import asyncio
```

At the very start of `_check_date()`, after the existing skip-cache check, add:

```python
        # Sniper interrupt: another date already found slots — skip immediately
        if abort_event is not None and abort_event.is_set():
            logger.debug(
                f"[check] {target_date.isoformat()} — skipped "
                "(first slot already found on another date)"
            )
            return []
```

After the navigation block (after `page.goto()` / `page.reload()`), add:

```python
            # Check for early abort before expensive calendar interaction
            if abort_event is not None and abort_event.is_set():
                return []
```

After `_wait_for_calendar()` returns True and before `_click_day()`, add:

```python
            if abort_event is not None and abort_event.is_set():
                return []
```

After slots are collected and sorted, just before `return self._sort_by_preferred_time(slots)`, add:

```python
            # Signal other concurrent tasks to abort now that we have slots
            if slots and abort_event is not None:
                abort_event.set()
                logger.info(
                    f"[check] {date_str} — first slot found, "
                    "abort signaled to remaining tasks"
                )
```

- [ ] **Step 4: Update `_scan_dates` in `check_all()` to create and pass the event**

In `check_all()`, inside the `_scan_dates` closure, change the `concurrent` branch:

Replace:
```python
                if concurrent:
                    results = await _asyncio.gather(
                        *[self._check_date(d, keep_page=keep_pages) for d in dates],
                        return_exceptions=True,
                    )
```

With:
```python
                if concurrent:
                    # Create abort event only in sniper mode (keep_pages=True).
                    # Non-sniper concurrent scans always complete all dates.
                    abort_evt = _asyncio.Event() if keep_pages else None
                    results = await _asyncio.gather(
                        *[
                            self._check_date(d, keep_page=keep_pages, abort_event=abort_evt)
                            for d in dates
                        ],
                        return_exceptions=True,
                    )
```

Also update the sequential branch to stop after first slot in sniper mode:

Replace:
```python
                else:
                    slots = []
                    for d in dates:
                        slots.extend(await self._check_date(d, keep_page=keep_pages))
                    return slots
```

With:
```python
                else:
                    slots = []
                    for d in dates:
                        result = await self._check_date(d, keep_page=keep_pages)
                        slots.extend(result)
                        if result and keep_pages:
                            logger.info(
                                "[check] First slot found — stopping sequential scan early"
                            )
                            break
                    return slots
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_interrupt_scan.py -v
```

Expected: 4 PASS

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/checker.py tests/test_interrupt_scan.py
git commit -m "fix: interrupt concurrent scan on first slot detection in sniper mode"
```

---

## Task 4: Fix booking click flow

**Context:** On April 10 the booker clicked a generic `button:visible:has-text("Book")` element — likely the restaurant-level "Book now" button, not a time-slot button — because `_click_time_slot()` falls back to the first matched button when no exact time match is found. Additionally, `_wait_for_checkout()` has a single 20s `wait_for_selector` with no intermediate checks. Fixes: (1) guard generic-"Book" button clicks — only click if parent container contains the target time string; (2) scroll-to-bottom after slot click so confirm button is visible; (3) replace single 20s wait with a 2s-polling loop up to 30s that checks selector + URL + payment element.

**Files:**
- Modify: `src/booker.py`
- Create: `tests/test_booking_fixes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_booking_fixes.py`:

```python
"""Tests for booking click flow fixes."""
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import date

from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


def _make_slot(slot_time="5:00 PM"):
    return AvailableSlot(
        slot_date=date(2026, 4, 17),
        slot_time=slot_time,
        day_of_week="Friday",
    )


@pytest.mark.asyncio
async def test_generic_book_button_skipped_when_no_time_in_parent():
    """A generic 'Book' button whose parent has no time text must NOT be clicked."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")

    page = AsyncMock()
    # wait_for_selector times out (no specific slot buttons)
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    # locator().count() returns 0 for specific selectors, 1 for generic
    def make_locator(selector):
        loc = MagicMock()
        if 'has-text("Book")' in selector or 'book_now' in selector.lower():
            loc.count = AsyncMock(return_value=1)
            btn = AsyncMock()
            btn.text_content = AsyncMock(return_value="Book")
            parent = AsyncMock()
            # Parent has NO time text — should skip this button
            parent.text_content = AsyncMock(return_value="Restaurant details")
            btn.locator = MagicMock(return_value=parent)
            loc.nth = MagicMock(return_value=btn)
        else:
            loc.count = AsyncMock(return_value=0)
        return loc

    page.locator = MagicMock(side_effect=make_locator)
    page.click = AsyncMock()

    result = await booker._click_time_slot(page, slot)

    # Should fail closed — not click the generic button
    assert result is False
    page.click.assert_not_called()


@pytest.mark.asyncio
async def test_generic_book_button_clicked_when_time_in_parent():
    """A generic 'Book' button whose parent contains the target time MUST be clicked."""
    booker = _make_booker()
    slot = _make_slot("5:00 PM")

    page = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    def make_locator(selector):
        loc = MagicMock()
        if 'has-text("Book")' in selector:
            loc.count = AsyncMock(return_value=1)
            btn = AsyncMock()
            btn.text_content = AsyncMock(return_value="Book")
            parent = AsyncMock()
            # Parent DOES contain the target time
            parent.text_content = AsyncMock(return_value="5:00 PM  Book  2 guests")
            btn.locator = MagicMock(return_value=parent)
            loc.nth = MagicMock(return_value=btn)
        else:
            loc.count = AsyncMock(return_value=0)
        return loc

    page.locator = MagicMock(side_effect=make_locator)

    result = await booker._click_time_slot(page, slot)

    assert result is True


@pytest.mark.asyncio
async def test_checkout_detection_polls_payment_element():
    """_wait_for_checkout falls back to payment-element detection within 30s."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"

    # selector wait always times out
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))

    # payment indicator found on 3rd attempt
    call_count = [0]
    async def mock_query_selector(selector):
        call_count[0] += 1
        if call_count[0] >= 3 and "Add payment" in selector:
            return MagicMock()  # payment element found
        return None

    page.query_selector = AsyncMock(side_effect=mock_query_selector)

    result = await booker._wait_for_checkout(page, slot)

    assert result is True


@pytest.mark.asyncio
async def test_checkout_detection_respects_url_change():
    """_wait_for_checkout detects checkout via URL containing '/checkout'."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()

    # Simulate URL change to checkout after 2s
    call_count = [0]
    async def mock_wait_for_selector(selector, timeout=None):
        call_count[0] += 1
        if call_count[0] >= 2:
            page.url = "https://www.exploretock.com/test/checkout/abc123"
        raise Exception("timeout")

    page.wait_for_selector = AsyncMock(side_effect=mock_wait_for_selector)
    page.url = "https://www.exploretock.com/test/search"
    page.query_selector = AsyncMock(return_value=None)

    result = await booker._wait_for_checkout(page, slot)

    assert result is True
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_booking_fixes.py -v
```

Expected: FAIL on generic-button and checkout tests.

- [ ] **Step 3: Update `_click_time_slot()` in `src/booker.py`**

In `src/booker.py`, locate `_click_time_slot()`. Find the variable `matched_selector` (set after iterating slot selectors). Add a module-level constant after the imports:

```python
# Selectors that match generic "Book" buttons (restaurant/experience level, not time-slot).
# These must only be clicked if surrounding context confirms the target time.
_GENERIC_BOOK_SELECTORS: frozenset[str] = frozenset({
    'button:visible:has-text("Book")',
    'button:text("Book now")',
    'a:text("Book now")',
    '[data-testid="book-now"]',
    "button.SearchExperience-bookButton",
    "[data-testid='book-button']",
})
```

In the button iteration loop inside `_click_time_slot()`, find the fallback section:

```python
        # Iterate buttons to find one matching slot.slot_time
        locator = page.locator(matched_selector)
        count = await locator.count()
        target_time = slot.slot_time.strip().upper()

        best_btn = None
        for i in range(count):
            btn = locator.nth(i)
            try:
                text = (await btn.text_content() or "").strip()
                if target_time in text.upper():
                    await btn.click()
                    logger.info(f"[book] Clicked slot button matching '{slot.slot_time}': {text}")
                    return True
                time_match = re.search(
                    r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', text, re.IGNORECASE
                )
                if time_match and time_match.group(1).strip().upper() == target_time:
                    await btn.click()
                    logger.info(f"[book] Clicked slot button (regex match): {text}")
                    return True
                if best_btn is None:
                    best_btn = btn
            except Exception:
                continue

        # Fallback: click first button
        if best_btn is not None:
            try:
                text = (await best_btn.text_content() or "").strip()
                await best_btn.click()
                logger.warning(
                    f"[book] No exact time match for '{slot.slot_time}' — "
                    f"clicked first button: {text}"
                )
                return True
            except Exception as e:
                logger.error(f"[book] Could not click fallback slot button: {e}")
                return False
```

Replace the entire loop body and fallback with:

```python
        # Iterate buttons to find one matching slot.slot_time
        locator = page.locator(matched_selector)
        count = await locator.count()
        target_time = slot.slot_time.strip().upper()
        is_generic = matched_selector in _GENERIC_BOOK_SELECTORS

        best_btn = None
        for i in range(count):
            btn = locator.nth(i)
            try:
                text = (await btn.text_content() or "").strip()

                # Exact time match in button text → click immediately
                if target_time in text.upper():
                    await btn.click()
                    logger.info(
                        f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                    )
                    return True

                # Regex time match in button text
                time_match = re.search(
                    r'\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b', text, re.IGNORECASE
                )
                if time_match and time_match.group(1).strip().upper() == target_time:
                    await btn.click()
                    logger.info(f"[book] Clicked slot button (regex match): {text}")
                    return True

                # Generic "Book" button: only click if parent container has target time.
                # This prevents clicking the restaurant-level "Book now" button by mistake.
                if is_generic:
                    try:
                        parent_text = (
                            await btn.locator("..").text_content() or ""
                        ).strip()
                    except Exception:
                        parent_text = ""
                    if target_time in parent_text.upper() or re.search(
                        r'\b' + re.escape(slot.slot_time) + r'\b',
                        parent_text, re.IGNORECASE
                    ):
                        await btn.click()
                        logger.info(
                            f"[book] Clicked generic 'Book' button — "
                            f"time confirmed in parent: {parent_text[:80]!r}"
                        )
                        return True
                    logger.debug(
                        f"[book] Generic button at index {i} skipped — "
                        f"no time match in parent: {parent_text[:80]!r}"
                    )
                    continue  # do NOT set best_btn for unmatched generic buttons

                if best_btn is None:
                    best_btn = btn
            except Exception:
                continue

        # Fallback: click first non-generic button (only reached for specific selectors)
        if best_btn is not None:
            try:
                text = (await best_btn.text_content() or "").strip()
                await best_btn.click()
                logger.warning(
                    f"[book] No exact time match for '{slot.slot_time}' — "
                    f"clicked first specific button: {text}"
                )
                return True
            except Exception as e:
                logger.error(f"[book] Could not click fallback slot button: {e}")
                return False

        logger.error(
            f"[book] No clickable slot button found for '{slot.slot_time}' "
            f"(selector: {matched_selector!r})"
        )
        return False
```

- [ ] **Step 4: Add scroll-to-bottom after `_click_time_slot()` in `_book_single()`**

In `_book_single()`, find:

```python
            if not await self._click_time_slot(page, slot):
                return False

            # _wait_for_checkout handles the timing — no need for a blind tick
```

Replace with:

```python
            if not await self._click_time_slot(page, slot):
                return False

            # Scroll to bottom so the confirm button (which may be below the fold
            # on a 800px viewport) becomes accessible before checkout detection.
            try:
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            except Exception:
                pass  # non-critical — proceed regardless
```

- [ ] **Step 5: Replace `_wait_for_checkout()` with polling version**

In `src/booker.py`, replace the entire `_wait_for_checkout()` method:

```python
    async def _wait_for_checkout(self, page: Page, slot: AvailableSlot) -> bool:
        """Return True when the checkout/booking-details page is detected.

        Polls every 2s for up to 30s, checking three signals in order:
          1. checkout_container selector present
          2. URL contains /checkout, /reservation, or /book
          3. Any payment-related element present (saved card or add-card prompt)
        """
        key = "checkout_container"
        selector = sel.get(key)
        no_pay_sel = sel.get("no_payment_indicator")
        saved_card_sel = sel.get("saved_payment_card")
        total_wait = 30
        interval = 2

        for elapsed in range(0, total_wait, interval):
            # 1. Checkout container selector
            try:
                await page.wait_for_selector(selector, timeout=interval * 1000)
                logger.info(
                    f"[book] Checkout page loaded for {slot.slot_date_str} "
                    f"(+{elapsed}s)"
                )
                return True
            except Exception:
                pass

            # 2. URL-based detection
            url = page.url
            if any(p in url for p in ("/checkout", "/reservation", "/book")):
                logger.info(f"[book] Checkout detected via URL: {url}")
                return True

            # 3. Payment element detection
            try:
                pay_el = await page.query_selector(no_pay_sel)
                if pay_el is None:
                    pay_el = await page.query_selector(saved_card_sel)
                if pay_el:
                    logger.info(
                        f"[book] Checkout detected via payment element "
                        f"at +{elapsed + interval}s"
                    )
                    return True
            except Exception:
                pass

            logger.debug(
                f"[book] Waiting for checkout… {elapsed + interval}s / {total_wait}s"
            )

        url = page.url
        logger.error(
            f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            f"  Checkout page not detected after {total_wait}s.\n"
            f"  Current URL: {url}\n"
            f"  → Update src/selectors.py"
        )
        return False
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
python -m pytest tests/test_booking_fixes.py -v
```

Expected: 4 PASS

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/booker.py tests/test_booking_fixes.py
git commit -m "fix: guard generic Book button clicks; scroll-to-bottom; polling checkout wait"
```

---

## Task 5: Screenshots during booking attempts

**Context:** When booking fails (wrong button click, checkout timeout), there's no visual evidence to diagnose what happened. `src/checker.py` already has debug screenshots but skips them in sniper mode. `src/booker.py` captures nothing. Fix: add a `_booking_screenshot()` helper to `TockBooker` that saves screenshots to `debug_screenshots/booking_<timestamp>_<step>.png`. Gated on `config.debug_screenshots` so it's zero-overhead in production.

**Files:**
- Modify: `src/booker.py`
- Modify: `tests/test_booking_fixes.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_booking_fixes.py`:

```python
import os
from pathlib import Path
from unittest.mock import patch


@pytest.mark.asyncio
async def test_screenshot_taken_on_checkout_timeout(tmp_path):
    """When debug_screenshots=True and checkout times out, a screenshot is saved."""
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=True,  # enabled
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    booker = TockBooker(config, browser, notifier)

    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    page.query_selector = AsyncMock(return_value=None)
    screenshot_paths = []

    async def mock_screenshot(path=None, **kwargs):
        if path:
            screenshot_paths.append(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"PNG")

    page.screenshot = AsyncMock(side_effect=mock_screenshot)

    slot = _make_slot()
    with patch("src.booker._SCREENSHOT_DIR", str(tmp_path)):
        await booker._wait_for_checkout(page, slot)

    # At least one screenshot should have been taken
    assert len(screenshot_paths) >= 1
    assert all("booking_" in p for p in screenshot_paths)


@pytest.mark.asyncio
async def test_no_screenshot_when_debug_disabled(tmp_path):
    """When debug_screenshots=False, no screenshots during booking."""
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,  # disabled
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    booker = TockBooker(config, browser, notifier)

    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    slot = _make_slot()
    with patch("src.booker._SCREENSHOT_DIR", str(tmp_path)):
        await booker._wait_for_checkout(page, slot)

    page.screenshot.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_booking_fixes.py::test_screenshot_taken_on_checkout_timeout tests/test_booking_fixes.py::test_no_screenshot_when_debug_disabled -v
```

Expected: FAIL (`_SCREENSHOT_DIR` not defined in booker, screenshot not called)

- [ ] **Step 3: Add screenshot infrastructure to `src/booker.py`**

After the existing imports at the top of `src/booker.py`, add:

```python
import os
from datetime import datetime as _datetime

_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "debug_screenshots"
)
os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
```

Add the helper method to `TockBooker` (anywhere in the class, after `_wait_for_checkout`):

```python
    async def _booking_screenshot(self, page: Page, step: str) -> None:
        """Save a screenshot at *step* during booking (only when debug_screenshots=True)."""
        if not self.config.debug_screenshots:
            return
        try:
            ts = _datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:19]
            path = os.path.join(_SCREENSHOT_DIR, f"booking_{ts}_{step}.png")
            await page.screenshot(path=path, full_page=True)
            logger.info(f"[book] Screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[book] Screenshot failed at step '{step}': {e}")
```

- [ ] **Step 4: Call `_booking_screenshot()` at four key moments in `_book_single()`**

In `_book_single()`, add calls at:

**a) Booking start** — add after `logger.info(f"[book] {slot} → using warm page (skipping navigation)")`:
```python
            await self._booking_screenshot(page, "01_booking_start")
```

(For both the warm-page and fresh-page branches, add this call just before Step 3.)

**b) After slot click** — add after the scroll-to-bottom call:
```python
            await self._booking_screenshot(page, "02_after_slot_click")
```

**c) After checkout wait** (regardless of result) — modify the checkout wait section:
```python
            checkout_ok = await self._wait_for_checkout(page, slot)
            await self._booking_screenshot(
                page,
                "03_checkout_loaded" if checkout_ok else "03_checkout_timeout"
            )
            if not checkout_ok:
                return False
```

- [ ] **Step 5: Call `_booking_screenshot()` on checkout timeout in `_wait_for_checkout()`**

At the end of `_wait_for_checkout()`, just before the `logger.error(...)` call, add:

```python
        await self._booking_screenshot(page, "checkout_timeout_final")
```

Wait — `_wait_for_checkout` is not `async` in terms of accessing `self.config`. It is `async`. But `_booking_screenshot` needs `self`. Since `_wait_for_checkout` is already a method of `TockBooker`, this works fine. Add the screenshot call inside the final block:

```python
        url = page.url
        # Capture final state for diagnosis
        await self._booking_screenshot(page, "checkout_timeout_final")
        logger.error(
            f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            ...
        )
        return False
```

- [ ] **Step 6: Run tests to confirm they pass**

```bash
python -m pytest tests/test_booking_fixes.py -v
```

Expected: All pass (6 total).

- [ ] **Step 7: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 8: Commit**

```bash
git add src/booker.py tests/test_booking_fixes.py
git commit -m "feat: add booking debug screenshots at key moments (gated on DEBUG_SCREENSHOTS)"
```

---

## Task 6: Suppress pre-booking Discord embed in sniper mode

**Context:** `monitor.poll()` calls `notifier.slots_found(slots)` which fires a Discord webhook embed before booking starts. In sniper mode, every millisecond counts; more importantly, a Discord notification implies "we found something, go look" but the bot is already booking — confusing. Fix: add `sniper_mode=False` parameter to `slots_found()`. Console log always prints; Discord embed is suppressed when `sniper_mode=True`.

**Files:**
- Modify: `src/notifier.py`
- Modify: `src/monitor.py`
- Modify: `tests/test_sniper_phases.py` (add tests)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_sniper_phases.py`:

```python
def test_slots_found_discord_suppressed_in_sniper(caplog):
    """slots_found(sniper_mode=True) must not call _fire() (Discord)."""
    from src.notifier import Notifier
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="https://discord.example.com/webhook",
        card_cvc="",
    )
    notifier = Notifier(config)
    fire_calls = []
    notifier._fire = lambda *a, **kw: fire_calls.append((a, kw))

    from src.checker import AvailableSlot
    from datetime import date
    slots = [AvailableSlot(
        slot_date=date(2026, 4, 17), slot_time="5:00 PM", day_of_week="Friday"
    )]
    notifier.slots_found(slots, sniper_mode=True)

    assert fire_calls == [], "Discord _fire must not be called in sniper mode"


def test_slots_found_discord_sent_outside_sniper():
    """slots_found(sniper_mode=False) MUST call _fire() (Discord notification)."""
    from src.notifier import Notifier
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False,
        discord_webhook_url="https://discord.example.com/webhook",
        card_cvc="",
    )
    notifier = Notifier(config)
    fire_calls = []
    notifier._fire = lambda *a, **kw: fire_calls.append((a, kw))

    from src.checker import AvailableSlot
    from datetime import date
    slots = [AvailableSlot(
        slot_date=date(2026, 4, 17), slot_time="5:00 PM", day_of_week="Friday"
    )]
    notifier.slots_found(slots, sniper_mode=False)

    assert len(fire_calls) == 1, "Discord _fire must be called outside sniper mode"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
python -m pytest tests/test_sniper_phases.py::test_slots_found_discord_suppressed_in_sniper tests/test_sniper_phases.py::test_slots_found_discord_sent_outside_sniper -v
```

Expected: FAIL (`slots_found` doesn't accept `sniper_mode` parameter yet)

- [ ] **Step 3: Update `slots_found()` in `src/notifier.py`**

Find `slots_found()`:

```python
    def slots_found(self, slots: list) -> None:
        lines = [f"• {s.slot_date_str} ({s.day_of_week}) @ {s.slot_time}" for s in slots[:8]]
        extra = f"\n+{len(slots) - 8} more…" if len(slots) > 8 else ""
        summary = "\n".join(lines) + extra
        logger.info(f"[slots] {len(slots)} slot(s) found:\n{summary}")
        self._fire(
            title=f"🟡 {len(slots)} Slot(s) Available!",
            description=summary,
            color=_YELLOW,
        )
```

Replace with:

```python
    def slots_found(self, slots: list, sniper_mode: bool = False) -> None:
        lines = [f"• {s.slot_date_str} ({s.day_of_week}) @ {s.slot_time}" for s in slots[:8]]
        extra = f"\n+{len(slots) - 8} more…" if len(slots) > 8 else ""
        summary = "\n".join(lines) + extra
        logger.info(f"[slots] {len(slots)} slot(s) found:\n{summary}")
        if sniper_mode:
            # In sniper mode the bot is already attempting to book — suppress
            # the Discord embed so the "slots found" and "booking confirmed"
            # don't arrive out of order or before booking completes.
            return
        self._fire(
            title=f"🟡 {len(slots)} Slot(s) Available!",
            description=summary,
            color=_YELLOW,
        )
```

- [ ] **Step 4: Pass `sniper_mode` in `monitor.poll()`**

In `src/monitor.py`, in `poll()`, find:

```python
        self.notifier.slots_found(slots)
```

Replace with:

```python
        self.notifier.slots_found(slots, sniper_mode=self._sniper_active)
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_sniper_phases.py -v
```

Expected: All pass (9 total).

- [ ] **Step 6: Run full suite**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 7: Commit**

```bash
git add src/notifier.py src/monitor.py tests/test_sniper_phases.py
git commit -m "fix: suppress Discord slots_found embed during sniper mode (book first, notify after)"
```

---

## Task 7: `--test-sniper-phases` integration test flag

**Context:** There's no way to dry-run the two-phase behavior (Phase 1 pre-release no-op → Phase 2 aggressive scan) without waiting for a real Friday sniper window. Add `--test-sniper-phases` to `main.py` that sets the sniper window 30s from now, forces sniper active, runs polls, and logs which phase each poll was in.

**Files:**
- Modify: `main.py`
- Modify: `src/testing/sniper_tests.py`

- [ ] **Step 1: Add test function to `src/testing/sniper_tests.py`**

Open `src/testing/sniper_tests.py` and add at the end of the file:

```python
async def test_sniper_phases(
    browser,
    config,
    notifier,
    checker,
    tracker,
    num_polls: int = 20,
) -> None:
    """
    Simulate the two-phase sniper: Phase 1 (pre-release no-ops) then Phase 2
    (aggressive scan). Sets the sniper window to start 30s from now so Phase 1
    runs for ~30s, then Phase 2 kicks in automatically.

    Prints a phase log so you can confirm:
      - Phase 1 polls: check_all returns [] immediately (pre-release)
      - Phase 2 polls: check_all scans calendars (aggressive)

    DRY_RUN is forced — no booking ever fires.
    """
    import asyncio
    from datetime import datetime, timedelta
    import pytz

    config.dry_run = True
    PT = pytz.timezone("America/Los_Angeles")
    now_pt = datetime.now(PT)

    # Set sniper to start 30s from now so we observe both phases
    start_time = now_pt + timedelta(seconds=30)
    config.sniper_days = [start_time.strftime("%A")]
    config.sniper_times = [start_time.strftime("%H:%M")]
    config.sniper_duration_min = 3  # 3 min window — enough to test both phases

    logger.info(
        f"\n{'='*60}\n"
        f"[test-sniper-phases] Two-phase sniper test\n"
        f"  Window start : {config.sniper_times[0]} PT (in ~30s)\n"
        f"  Phase 1 ends : +60s after window start (pre-release no-op)\n"
        f"  Phase 2 starts: +60s (aggressive calendar scan)\n"
        f"  Polls        : {num_polls}\n"
        f"  Booking      : DISABLED (DRY_RUN)\n"
        f"{'='*60}"
    )

    from src.monitor import TockMonitor
    monitor = TockMonitor(config, browser, checker, notifier, tracker)

    # Manually drive polls, sleeping 3s between each
    for i in range(1, num_polls + 1):
        # Calculate what phase we'd be in
        now_pt = datetime.now(PT)
        sniper_start = now_pt.replace(
            hour=int(config.sniper_times[0].split(":")[0]),
            minute=int(config.sniper_times[0].split(":")[1]),
            second=0, microsecond=0
        )
        sniper_age = max(0.0, (now_pt - sniper_start).total_seconds())
        in_window = 0 <= sniper_age < config.sniper_duration_min * 60

        if in_window and sniper_age < 60:
            phase = "PHASE-1 (pre-release no-op)"
        elif in_window:
            phase = "PHASE-2 (aggressive scan)"
        else:
            phase = "PRE-WINDOW (not in sniper)"

        logger.info(f"[test-sniper-phases] ── Poll {i}/{num_polls} [{phase}] ──")
        await monitor.poll()
        await asyncio.sleep(3)

    logger.info(
        f"\n{'='*60}\n"
        f"[test-sniper-phases] Done.\n"
        f"  Review log for PHASE-1 (no calendar scans) and\n"
        f"  PHASE-2 (calendar scans with abort-on-first-slot).\n"
        f"{'='*60}"
    )
```

- [ ] **Step 2: Add `--test-sniper-phases` argument and dispatch in `main.py`**

In `main.py`, in `async def main()`, find the argument parser section. After the `--test-adaptive-sniper` argument block, add:

```python
    parser.add_argument(
        "--test-sniper-phases",
        action="store_true",
        help=(
            "Test two-phase sniper: sets window 30s from now, runs pre-release "
            "Phase 1 no-ops then Phase 2 aggressive scans. DRY_RUN forced. "
            "Use --test-sniper-polls N to control poll count (default: 20)."
        ),
    )
```

In the mode-dispatch section (after `if args.test_adaptive_sniper:`), add:

```python
        # ── Mode: --test-sniper-phases ────────────────────────────────────
        if args.test_sniper_phases:
            if not await browser.login():
                logger.error("Login failed — cannot run --test-sniper-phases.")
                sys.exit(1)
            from src.testing.sniper_tests import test_sniper_phases
            await test_sniper_phases(
                browser, config, notifier, checker, tracker,
                num_polls=args.test_sniper_polls,
            )
            return
```

Also add the import at the top of `main.py` (in the existing testing imports block):

```python
from src.testing.sniper_tests import (
    test_sniper_benchmark,
    test_sniper_integration,
    test_sniper_robustness,
    test_sniper_phases,
)
```

- [ ] **Step 3: Verify the flag is registered correctly (smoke test)**

```bash
python main.py --help | grep test-sniper-phases
```

Expected output contains: `--test-sniper-phases`

- [ ] **Step 4: Run full suite to confirm no regressions**

```bash
python -m pytest tests/ -q
```

Expected: All pass.

- [ ] **Step 5: Commit**

```bash
git add main.py src/testing/sniper_tests.py
git commit -m "feat: add --test-sniper-phases flag for testing two-phase sniper behavior"
```

---

## Self-Review

**Spec coverage check:**

| Requirement | Task |
|------------|------|
| Fix 1: Two-phase (Phase 1 light, Phase 2 aggressive) | Task 2 |
| Fix 2: Interrupt scan on first detection | Task 3 |
| Fix 3: No degradation before release | Task 1 |
| Fix 4: Fix booking click flow (guard generic button, scroll, checkout timeout 30s) | Task 4 |
| Fix 5: Screenshots during booking | Task 5 |
| Fix 6: Detection→booking latency (suppress Discord before booking) | Task 6 |
| `--test-sniper-phases` flag | Task 7 |

**Placeholder scan:** No TBDs or "implement later" — all steps contain complete code.

**Type consistency check:**
- `_apply_adaptive_switching(sniper_age: float)` — used in Task 1 tests and Task 1 implementation consistently.
- `_check_date(..., abort_event: asyncio.Event | None = None)` — used in Task 3 test mock and Task 3 implementation consistently.
- `slots_found(slots: list, sniper_mode: bool = False)` — used in Task 6 tests and implementation consistently.
- `_SCREENSHOT_DIR` — module-level constant in `booker.py`, patched via `src.booker._SCREENSHOT_DIR` in Task 5 tests.
- `test_sniper_phases` function name — matches import in `main.py`.

**Interaction check:** Tasks 1 and 2 interact (both read `sniper_window_age_sec` / `sniper_age`). They are independent changes (Task 1 is in `monitor.py`, Task 2 is in `checker.py`) and don't conflict. Tasks 4 and 5 both modify `booker.py` — the plan separates them by functionality and avoids conflicting line edits.
