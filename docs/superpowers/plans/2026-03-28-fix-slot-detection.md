# Fix Slot Detection — Bypass is-available Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the checker so it detects available slots during Tock release windows by bypassing the broken `is-available` CSS class gate and adding "Book now" button detection.

**Architecture:** The `/search?date=...` URL renders a restaurant page with a calendar modal. The calendar day buttons do NOT use `is-available` class — they're just visually dark (available) vs greyed (unavailable). The fix: (1) click the target day by number regardless of CSS class, (2) after clicking, check for both time slot buttons AND a "Book now" button, (3) if "Book now" is found, click it to proceed to the booking page where time slots appear.

**Tech Stack:** Python, Playwright, pytest, asyncio

---

### Task 1: Add new selectors

**Files:**
- Modify: `src/selectors.py:62-79`

- [ ] **Step 1: Write failing test**

```python
# tests/test_selectors.py
from src.selectors import get

def test_all_day_button_selector_exists():
    """all_day_button selector should match any in-month calendar day."""
    sel = get("all_day_button")
    assert "ConsumerCalendar-day" in sel
    assert "is-available" not in sel  # must NOT require is-available

def test_book_now_button_selector_exists():
    """book_now_button selector should exist."""
    sel = get("book_now_button")
    assert "Book now" in sel or "book" in sel.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_selectors.py -v`
Expected: FAIL with `KeyError: "Unknown selector key 'all_day_button'"`

- [ ] **Step 3: Add selectors to selectors.py**

In `src/selectors.py`, after the `available_day_button` entry, add:

```python
    # ANY clickable day button in the current month (not filtered by availability).
    # Used by Approach A: click the day by number, check if slots/Book-now appear.
    "all_day_button": "button.ConsumerCalendar-day.is-in-month",

    # "Book now" action button on the restaurant/search page.
    # Visible when at least one slot is available for the experience.
    "book_now_button": (
        'button:text("Book now"), '
        'a:text("Book now"), '
        '[data-testid="book-now"]'
    ),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_selectors.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/selectors.py tests/test_selectors.py
git commit -m "Add all_day_button and book_now_button selectors"
```

---

### Task 2: Rewrite _click_day to use all_day_button

**Files:**
- Modify: `src/checker.py` — `_click_day` method (~line 413)
- Test: `tests/test_checker_detection.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_checker_detection.py
"""Tests for the rewritten slot detection flow in AvailabilityChecker."""

import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


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
        fallback_days=[],
        preferred_time="17:00",
        scan_weeks=2,
        release_window_days=["Monday"],
        release_window_start="09:00",
        release_window_end="11:00",
        sniper_days=["Friday"],
        sniper_times=["19:59"],
        sniper_duration_min=11,
        sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_checker(**config_overrides) -> AvailabilityChecker:
    cfg = _make_config(**config_overrides)
    browser = MagicMock()
    tracker = MagicMock()
    tracker.record = MagicMock()
    return AvailabilityChecker(cfg, browser, tracker)


def _make_mock_button(text: str, disabled: bool = False):
    """Create a mock Playwright ElementHandle for a calendar day button."""
    btn = AsyncMock()
    btn.text_content = AsyncMock(return_value=text)
    btn.click = AsyncMock()
    btn.get_attribute = AsyncMock(side_effect=lambda attr: (
        "true" if attr == "disabled" and disabled
        else "ConsumerCalendar-day is-in-month" if attr == "class"
        else None
    ))
    btn.is_disabled = AsyncMock(return_value=disabled)
    return btn


class TestClickDayUsesAllButtons:
    """_click_day should find and click the target day by number
    using all_day_button (not filtered by is-available)."""

    @pytest.mark.asyncio
    async def test_clicks_matching_day_number(self):
        """Should click button with matching day number text."""
        checker = _make_checker()
        page = AsyncMock()

        btn3 = _make_mock_button("3")
        btn4 = _make_mock_button("4")
        btn5 = _make_mock_button("5")
        page.query_selector_all = AsyncMock(return_value=[btn3, btn4, btn5])

        target = date(2026, 4, 4)
        result = await checker._click_day(page, target)

        assert result is True
        btn4.click.assert_called_once()
        btn3.click.assert_not_called()
        btn5.click.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_false_when_day_not_found(self):
        """If target day number is not in any button, return False."""
        checker = _make_checker()
        page = AsyncMock()

        btn1 = _make_mock_button("1")
        btn2 = _make_mock_button("2")
        page.query_selector_all = AsyncMock(return_value=[btn1, btn2])

        target = date(2026, 4, 15)
        result = await checker._click_day(page, target)

        assert result is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_checker_detection.py::TestClickDayUsesAllButtons -v`
Expected: FAIL — `_click_day` currently uses `available_day_button` selector which won't match our mocks

- [ ] **Step 3: Update _click_day to use all_day_button**

In `src/checker.py`, change `_click_day`:

```python
    async def _click_day(self, page: Page, target_date: date) -> bool:
        """Click the calendar button for target_date. Returns True on success.

        Uses all_day_button (any in-month day) instead of available_day_button
        so we don't miss days that are available but lack the is-available class.
        """
        key = "all_day_button"
        selector = sel.get(key)
        target_num = str(target_date.day)

        day_buttons = await page.query_selector_all(selector)
        for btn in day_buttons:
            try:
                text = (await btn.text_content() or "").strip()
                if text == target_num:
                    await btn.click()
                    logger.info(
                        f"[check] Clicked day {target_num} for {target_date.isoformat()}"
                    )
                    return True
            except Exception:
                continue

        logger.warning(
            f"[check] Could not click day {target_num} for {target_date.isoformat()}\n"
            f"  SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            f"  → Update src/selectors.py"
        )
        return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_checker_detection.py::TestClickDayUsesAllButtons -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/checker.py tests/test_checker_detection.py
git commit -m "Change _click_day to use all_day_button selector"
```

---

### Task 3: Rewrite _check_date to remove is-available gate, add Book Now

**Files:**
- Modify: `src/checker.py` — `_check_date` method (~line 258–319)
- Test: `tests/test_checker_detection.py` (append)

- [ ] **Step 1: Write failing tests**

Append to `tests/test_checker_detection.py`:

```python
class TestCheckDateBypassesIsAvailable:
    """_check_date should NOT gate on is-available class.
    It should click the day by number and check for slots or Book Now."""

    @pytest.mark.asyncio
    async def test_finds_slots_without_is_available_class(self):
        """Day has no is-available class but slots exist after clicking → detected."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        # Calendar container loads OK
        page.wait_for_selector = AsyncMock(return_value=True)

        # Day button with matching number (no is-available class)
        btn4 = _make_mock_button("4")

        # First query_selector_all call: pre-loaded slot check (returns [])
        # Second: all_day_button for class dump
        # Third: all_day_button for clicking
        # Fourth: slot buttons after click
        slot_btn = AsyncMock()
        time_span = AsyncMock()
        time_span.text_content = AsyncMock(return_value="5:00 PM")
        slot_btn.query_selector = AsyncMock(return_value=time_span)

        call_count = [0]
        async def mock_query_all(selector):
            call_count[0] += 1
            if "resultsListItem" in selector:
                # First call for pre-loaded slots: empty
                # Later call after day click: has slots
                if call_count[0] <= 2:
                    return []
                return [slot_btn]
            if "ConsumerCalendar-day" in selector:
                return [btn4]
            return []

        page.query_selector_all = mock_query_all
        page.screenshot = AsyncMock()

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert len(slots) == 1
        assert slots[0].slot_time == "5:00 PM"
        assert slots[0].slot_date == target

    @pytest.mark.asyncio
    async def test_clicks_book_now_when_no_direct_slots(self):
        """After clicking day, no slot buttons but Book Now visible → click it."""
        checker = _make_checker()
        page = AsyncMock()
        page.is_closed = MagicMock(return_value=False)
        checker.browser.new_page = AsyncMock(return_value=page)

        # Calendar loads, day button exists
        btn4 = _make_mock_button("4")

        # After Book Now click, slots appear
        slot_btn = AsyncMock()
        time_span = AsyncMock()
        time_span.text_content = AsyncMock(return_value="8:00 PM")
        slot_btn.query_selector = AsyncMock(return_value=time_span)

        book_now_btn = AsyncMock()

        call_count = [0]
        async def mock_query_all(selector):
            call_count[0] += 1
            if "resultsListItem" in selector:
                # No pre-loaded slots, no slots after day click,
                # slots appear after Book Now click
                if call_count[0] >= 6:
                    return [slot_btn]
                return []
            if "ConsumerCalendar-day" in selector:
                return [btn4]
            return []

        page.query_selector_all = mock_query_all
        page.query_selector = AsyncMock(return_value=book_now_btn)
        page.screenshot = AsyncMock()

        # wait_for_selector: calendar OK, pre-loaded slots timeout,
        # slot buttons timeout, Book Now found, post-BookNow slots found
        wait_call = [0]
        async def mock_wait(selector, **kwargs):
            wait_call[0] += 1
            if "Book now" in selector or "book-now" in selector:
                return book_now_btn
            if "resultsListItem" in selector:
                if wait_call[0] >= 6:
                    return slot_btn
                raise TimeoutError("no slots yet")
            return True  # calendar container

        page.wait_for_selector = mock_wait

        target = date(2026, 4, 4)
        slots = await checker._check_date(target)

        assert len(slots) >= 1
        book_now_btn.click.assert_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_checker_detection.py::TestCheckDateBypassesIsAvailable -v`
Expected: FAIL — current `_check_date` still gates on `_is_day_available`

- [ ] **Step 3: Rewrite the _check_date detection flow**

Replace the Signal 2 section in `_check_date` (from `# --- Signal 2` to the `slots = await self._collect_slots` line) with:

```python
            # --- Signal 2: Click the day by number, then check for slots ---
            # Skip the is-available class gate entirely — Tock's modal calendar
            # doesn't reliably use it. Instead, click the target day by number
            # and check if slots or a "Book now" button appear.
            clicked = await self._click_day(page, target_date)
            if not clicked:
                logger.info(f"[check] {date_str} — could not click day in calendar")
                return []

            # After clicking the day, wait for either slot buttons or Book Now
            try:
                await page.wait_for_selector(
                    sel.get("available_slot_button"), timeout=3000
                )
            except Exception:
                pass  # no slot buttons; check for Book Now path

            slots = await self._collect_slots(page, target_date)

            # If no direct slot results, check for "Book now" button
            if not slots:
                try:
                    book_now = await page.query_selector(sel.get("book_now_button"))
                    if book_now:
                        logger.info(f"[check] {date_str} — 'Book now' button found, clicking")
                        await book_now.click()
                        # Wait for the booking/slot selection page to load
                        try:
                            await page.wait_for_selector(
                                sel.get("available_slot_button"), timeout=5000
                            )
                        except Exception:
                            pass
                        slots = await self._collect_slots(page, target_date)
                except Exception as e:
                    logger.debug(f"[check] {date_str} — Book Now check failed: {e}")
```

Also remove the now-unused `_is_day_available` gate (keep the method for debug class dump logging but don't use it as a gate — move the class dump into `_check_date` directly or keep `_is_day_available` as a debug-only logger).

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_checker_detection.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/checker.py tests/test_checker_detection.py
git commit -m "Remove is-available gate, add Book Now detection path"
```

---

### Task 4: Run full test suite

**Files:** None (verification only)

- [ ] **Step 1: Run all tests**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/ -v`
Expected: ALL PASS (both new and existing tests)

- [ ] **Step 2: Verify no regressions in existing sniper tests**

Run: `cd ~/tock-reservation-bot && python -m pytest tests/test_monitor_sniper.py -v`
Expected: ALL PASS

- [ ] **Step 3: Final commit and push**

```bash
git push origin main
```

---

### Summary of changes

| File | Change |
|------|--------|
| `src/selectors.py` | Add `all_day_button` and `book_now_button` selectors |
| `src/checker.py` | `_click_day`: use `all_day_button` instead of `available_day_button` |
| `src/checker.py` | `_check_date`: remove `_is_day_available` gate, add "Book now" click path |
| `tests/test_selectors.py` | New: selector existence tests |
| `tests/test_checker_detection.py` | New: detection flow tests (click by number, Book Now path) |
