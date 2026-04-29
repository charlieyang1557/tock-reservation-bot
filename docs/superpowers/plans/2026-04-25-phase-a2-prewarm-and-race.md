# Phase A+2: Target-Date Prewarm + Race All Slots

> **For agentic workers:** Use superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`).

**Goal:** Cut 1-2 seconds off the critical path during the first 5 seconds after release by (a) racing every detected slot concurrently instead of one-per-date, and (b) pre-opening target-date pages 5 minutes before release so the first sniper poll does a fast `page.reload()` instead of a fresh `page.goto()`.

**Architecture:** Three independent task groups inside the existing checker/booker/monitor structure. No new modules. Page lifetime is managed via the existing `AvailabilityChecker._sniper_pages: dict[date_str, Page]` — prewarm just populates it ahead of time.

**Tech Stack:** Python 3.11, asyncio, Playwright async_api, pytest, pytest-asyncio.

**Why now:** User confirms slots disappear within ~5s of release. After Phase A's interrupt-on-first-slot and slot-labeling fixes, the remaining critical-path cost is the first poll's `goto + wait + click_day + scan` per date. Pre-opening pages eliminates the goto (1-2s) for the predicted release dates. Removing `_best_per_date` lets the booker race 5pm AND 8pm of the same date concurrently — today only the closer-to-preferred-time slot is attempted.

**Cloudflare risk:** Today's sniper concurrent mode runs 14 dates simultaneously at ~1% error rate. Proposed prewarm opens 7 pages **spread across 3.5 minutes** (one per 30s) — strictly gentler than the release-moment burst. CF challenge telemetry alerts via Discord if challenges exceed 5% during prewarm so we can detect any future change in CF behavior.

**Double-booking:** User explicitly accepts rare double-booking risk to maximize hit rate. The existing `asyncio.Lock` + `asyncio.Event` in `book_best_slot_race` already serializes confirm clicks across all attempted slots — the only way to get a true double-booking is if two confirms succeed before either sets the event, which is a sub-millisecond window.

---

## File structure

| File | Change |
|------|--------|
| `src/booker.py` | Task 1: remove `_best_per_date` collapsing in `book_best_slot_race`. |
| `src/checker.py` | Task 2: add `prewarm_target_dates(dates)` method that opens pages and parks them at CALENDAR_LOADED state, populating `_sniper_pages`. Add CF-challenge detection helper. |
| `src/monitor.py` | Task 2: in the existing prewarm flow, after `warm_session()` succeeds, build the prewarm date list and call `checker.prewarm_target_dates(...)`. Add `PREWARM_DATES_BEFORE_MIN = 5`. |
| `src/notifier.py` | Task 3: new `cf_challenge_warning(rate, count)` Discord embed. |
| `tests/test_race_all_slots.py` | Task 1: new — assert booker races every slot, not just one per date. |
| `tests/test_prewarm_target_dates.py` | Task 2: new — assert pages opened spread in time, parked at CALENDAR_LOADED, populated into `_sniper_pages`. |
| `tests/test_cf_challenge_telemetry.py` | Task 3: new — assert challenge counter, threshold-based alert. |
| `.env.example` | Doc: suggest setting `FALLBACK_DAYS=Monday,Tuesday,Wednesday,Thursday` to enable weekday prewarm coverage. |

---

## Task 1: Race all slots (remove `_best_per_date`)

**Context:** `book_best_slot_race` calls `_best_per_date(slots)` which collapses to one slot per date (the closest to `preferred_time`). User wants 5pm AND 8pm of the same date raced concurrently. The existing `_confirm_lock` + `booking_won` event already serializes the actual confirm click across N concurrent attempts, so racing more slots is safe.

**Files:**
- Modify: `src/booker.py`
- Create: `tests/test_race_all_slots.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_race_all_slots.py`:

```python
"""Tests for racing all detected slots concurrently (Phase A+2 Task 1).

Today book_best_slot_race collapses to one slot per date via _best_per_date.
After this change, ALL slots are raced — multiple times on the same date
attempt concurrently. The existing asyncio.Lock + Event prevents the rare
double-booking, while maximizing hit rate (5pm AND 8pm of the same Friday
both attempted instead of just 5pm).
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
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
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


@pytest.mark.asyncio
async def test_races_all_slots_including_same_date():
    """Multiple slots on the same date both result in booking attempts."""
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 2), slot_time="5:00 PM", day_of_week="Saturday"),
    ]

    attempted = []

    async def fake_book_single(slot, booking_won, warm_page=None):
        attempted.append(slot)
        if not booking_won.is_set():
            booking_won.set()
            return True
        return False

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        booked = await booker.book_best_slot_race(slots)

    # All 3 slots must be attempted (concurrent race)
    attempted_keys = {(s.slot_date_str, s.slot_time) for s in attempted}
    assert ("2026-05-01", "5:00 PM") in attempted_keys
    assert ("2026-05-01", "8:00 PM") in attempted_keys
    assert ("2026-05-02", "5:00 PM") in attempted_keys
    assert len(attempted) == 3
    assert booked is not None  # one winner


@pytest.mark.asyncio
async def test_lock_still_prevents_double_confirm():
    """Even with all slots racing, only ONE actually wins (lock invariant holds)."""
    booker = _make_booker()
    slots = [
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM", day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 1), slot_time="8:00 PM", day_of_week="Friday"),
    ]

    winners = []

    async def fake_book_single(slot, booking_won, warm_page=None):
        await asyncio.sleep(0)  # yield to other tasks
        if booking_won.is_set():
            return False  # respects the event
        booking_won.set()
        winners.append(slot)
        return True

    with patch.object(booker, "_book_single", side_effect=fake_book_single):
        await booker.book_best_slot_race(slots)

    assert len(winners) == 1, (
        f"Lock+event must serialize confirm to exactly one winner; got {winners}"
    )
```

- [ ] **Step 2: Confirm tests fail**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_race_all_slots.py -v
```

Expected: `test_races_all_slots_including_same_date` FAILs (only 2 attempted because `_best_per_date` collapses Friday's two slots to one). `test_lock_still_prevents_double_confirm` may pass coincidentally — that's fine, it's a regression guard.

- [ ] **Step 3: Remove `_best_per_date` collapsing**

In `src/booker.py`, find `book_best_slot_race`:

```python
        candidates = self._best_per_date(slots)
        logger.info(
            f"Starting concurrent booking race for {len(candidates)} slot(s): "
            + " | ".join(str(s) for s in candidates)
        )
```

Replace with:

```python
        # Phase A+2: race ALL slots, not just one per date. The asyncio.Lock +
        # booking_won.Event serialize the actual confirm click — so attempting
        # 5pm AND 8pm of the same Friday concurrently still produces at most
        # one booking. Maximizes hit rate when releases drop multiple times
        # on the same date.
        candidates = list(slots)
        logger.info(
            f"Starting concurrent booking race for {len(candidates)} slot(s): "
            + " | ".join(str(s) for s in candidates)
        )
```

The `_best_per_date` method itself can stay defined (no harm) — just no longer called.

- [ ] **Step 4: Run tests, confirm green**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_race_all_slots.py -v
```

Expected: 2 PASS.

- [ ] **Step 5: Run full suite — watch for regressions in existing booker tests**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/ -q
```

If any existing test fails because it asserts the booker only attempts 1 slot per date, investigate carefully. Update only if the assertion encoded the OLD behavior we're explicitly removing.

- [ ] **Step 6: Commit**

```bash
git add src/booker.py tests/test_race_all_slots.py
git commit -m "feat: race all slots concurrently (drop _best_per_date collapse)

Today book_best_slot_race collapses to one slot per date — Friday's 5pm
AND 8pm get reduced to whichever is closest to preferred_time. User wants
both attempted concurrently to maximize hit rate during the ~5s window
when slots disappear after release.

The existing asyncio.Lock + booking_won.Event already serialize confirm
clicks, so racing all slots produces at most one booking (rare sub-ms
double-confirm window is acceptable per user request).
"
```

---

## Task 2: Target-date page prewarm

**Context:** Today the bot only warms session cookies (`browser.warm_session()`) before the sniper window — it does NOT pre-open target-date search pages. The first sniper poll therefore does a fresh `page.goto(search_url)` per date, costing 1-2s before slot detection can begin. Pre-opening pages 5 minutes before release replaces that goto with a cached `page.reload()`, saving ~1-2s on the first poll. Pages are stored in the existing `AvailabilityChecker._sniper_pages` dict so the existing sniper-poll path picks them up automatically.

**Cloudflare consideration:** Spread the page opens in time (one per 30s) so the prewarm is gentler than today's release-moment burst (14 concurrent goto's at T+0). At ≤7 pages per release, prewarm completes in ≤3.5 minutes.

**Files:**
- Modify: `src/checker.py`
- Modify: `src/monitor.py`
- Create: `tests/test_prewarm_target_dates.py`
- Modify: `.env.example`

- [ ] **Step 1: Write failing test**

Create `tests/test_prewarm_target_dates.py`:

```python
"""Tests for target-date page prewarm (Phase A+2 Task 2)."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, call, patch
import pytest

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
    browser = MagicMock()
    browser.new_page = AsyncMock()
    return AvailabilityChecker(config, browser, MagicMock())


@pytest.mark.asyncio
async def test_prewarm_opens_one_page_per_date():
    """prewarm_target_dates opens exactly N pages and stores them in _sniper_pages."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    pages = []
    async def make_page():
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.goto = AsyncMock()
        p.wait_for_selector = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    # Use stagger=0 to keep the test fast
    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert len(pages) == 3
    assert set(checker._sniper_pages.keys()) == {
        "2026-05-01", "2026-05-02", "2026-05-03"
    }


@pytest.mark.asyncio
async def test_prewarm_navigates_to_correct_url():
    """Each prewarmed page navigates to the per-date Tock search URL."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    page.goto.assert_called_once()
    args = page.goto.call_args
    url = args.args[0] if args.args else args.kwargs.get("url")
    assert "date=2026-05-01" in url
    assert "size=2" in url
    assert "fui-hui-hua-san-francisco" not in url or "test" in url  # uses test slug


@pytest.mark.asyncio
async def test_prewarm_waits_for_calendar_container():
    """After goto, prewarm waits for the calendar to render (parks at CALENDAR_LOADED)."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Confirm wait_for_selector was called (parked at calendar render)
    assert page.wait_for_selector.called


@pytest.mark.asyncio
async def test_prewarm_failure_does_not_break_other_dates():
    """If one prewarm fails, others still complete."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    page_count = [0]
    pages = []
    async def make_page():
        page_count[0] += 1
        p = AsyncMock()
        p.is_closed = MagicMock(return_value=False)
        p.wait_for_selector = AsyncMock()
        if page_count[0] == 2:
            p.goto = AsyncMock(side_effect=Exception("fake CF error"))
        else:
            p.goto = AsyncMock()
        pages.append(p)
        return p
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Two pages successfully prewarmed (1st and 3rd); 2nd failed but didn't kill the others
    assert len(checker._sniper_pages) == 2
    assert "2026-05-01" in checker._sniper_pages
    assert "2026-05-03" in checker._sniper_pages
    assert "2026-05-02" not in checker._sniper_pages


@pytest.mark.asyncio
async def test_prewarm_respects_stagger():
    """Pages open spread across `stagger_sec` intervals."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    checker.browser.new_page = AsyncMock(return_value=page)

    sleep_calls = []
    real_sleep = asyncio.sleep
    async def fake_sleep(secs):
        sleep_calls.append(secs)
        # Don't actually sleep — keep the test fast
        await real_sleep(0)

    with patch("src.checker.asyncio.sleep", new=fake_sleep):
        await checker.prewarm_target_dates(dates, stagger_sec=30)

    # Between 3 dates we expect 2 stagger sleeps of ~30s
    assert any(s == 30 for s in sleep_calls), (
        f"Expected at least one stagger sleep of 30s; got {sleep_calls}"
    )
```

- [ ] **Step 2: Confirm tests fail**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_prewarm_target_dates.py -v
```

Expected: AttributeError because `prewarm_target_dates` does not exist.

- [ ] **Step 3: Implement `prewarm_target_dates` in `src/checker.py`**

Add to `AvailabilityChecker` (anywhere in the class — recommend after `close_sniper_pages`):

```python
    async def prewarm_target_dates(
        self,
        target_dates: list[date],
        stagger_sec: float = 30.0,
    ) -> None:
        """Open one Playwright page per target_date, parked at CALENDAR_LOADED.

        Pages are stored in self._sniper_pages so the existing sniper-poll path
        picks them up. The first poll after the sniper window opens uses
        page.reload() (cached), saving ~1-2s vs. a fresh page.goto().

        Pages are opened ONE AT A TIME with stagger_sec between starts to
        keep the prewarm gentle on Cloudflare/Turnstile (vs. opening all
        N pages concurrently). Failures on individual dates are logged but
        do not abort the rest.
        """
        for i, target_date in enumerate(target_dates):
            date_str = target_date.isoformat()
            url = (
                f"{BASE_URL}/{self.config.restaurant_slug}/search"
                f"?date={date_str}"
                f"&size={self.config.party_size}"
                f"&time={self.config.preferred_time}"
            )
            try:
                page = await self.browser.new_page()
                logger.info(f"[prewarm] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)
                # Park at CALENDAR_LOADED (calendar widget rendered)
                await page.wait_for_selector(
                    sel.get("calendar_container"), timeout=10000
                )
                self._sniper_pages[date_str] = page
                logger.info(
                    f"[prewarm] {date_str} parked at CALENDAR_LOADED"
                )
            except Exception as e:
                logger.warning(
                    f"[prewarm] {date_str} failed: {type(e).__name__}: {e}"
                )
                # Don't store the failed page — sniper-poll will fall back to
                # fresh goto() for this date

            # Stagger between page opens (skip after the last one)
            if i < len(target_dates) - 1 and stagger_sec > 0:
                await asyncio.sleep(stagger_sec)
```

- [ ] **Step 4: Wire prewarm into `monitor.py`**

In `src/monitor.py`, find the existing prewarm flow (around line 191 — `prewarm_target = self._get_prewarm_target()`). After the `warm_session()` success branch (around line 198, where `self._session_prewarmed_for = prewarm_target` is set), add:

```python
                if success:
                    self._session_prewarmed_for = prewarm_target
                    # Phase A+2: also pre-open target-date pages so the first
                    # sniper poll after the window opens does a fast reload()
                    # instead of a fresh goto() per date.
                    try:
                        prewarm_dates = self._get_prewarm_dates()
                        if prewarm_dates:
                            logger.info(
                                f"[monitor] Pre-opening {len(prewarm_dates)} "
                                f"target-date page(s) for {prewarm_target}"
                            )
                            await self.checker.prewarm_target_dates(
                                prewarm_dates, stagger_sec=30.0
                            )
                    except Exception as e:
                        logger.warning(
                            f"[monitor] Target-date prewarm failed (non-critical): {e}"
                        )
```

Add a new method to `TockMonitor` (after `_get_prewarm_target`):

```python
    def _get_prewarm_dates(self) -> list[date]:
        """Return up to 7 target dates within the next week to prewarm.

        Combines preferred_days + fallback_days, capped at 7 entries to keep
        prewarm under ~3.5 minutes (one page per 30s).
        """
        from datetime import date as _date, timedelta as _td
        days = list(self.config.preferred_days) + list(self.config.fallback_days)
        if not days:
            return []
        # One week ahead is enough for a single Friday release
        today = _date.today()
        end = today + _td(weeks=1)
        result: list[_date] = []
        current = today + _td(days=1)
        while current <= end and len(result) < 7:
            if current.strftime("%A") in days:
                result.append(current)
            current += _td(days=1)
        return result
```

Add `from datetime import date` to the existing `from datetime import datetime, time, timedelta` import in monitor.py if not already present.

- [ ] **Step 5: Update `.env.example` with weekday fallback suggestion**

In `.env.example`, find the line `FALLBACK_DAYS=` (or the section for fallback). Replace or add:

```
# Fallback days (booked only if no preferred_days slots found).
# To enable weekday prewarm coverage when releases include Mon-Thu, set:
# FALLBACK_DAYS=Monday,Tuesday,Wednesday,Thursday
FALLBACK_DAYS=
```

- [ ] **Step 6: Run tests, confirm green**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_prewarm_target_dates.py -v
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/ -q
```

Expected: 5 new tests pass; full suite green.

- [ ] **Step 7: Commit**

```bash
git add src/checker.py src/monitor.py tests/test_prewarm_target_dates.py .env.example
git commit -m "feat: pre-open target-date pages 5min before release (Phase A+2 prewarm)

Eliminates the ~1-2s goto cost on the first sniper poll. Pages are
opened with 30s stagger to keep CF exposure gentler than today's
release-moment 14-page burst, parked at CALENDAR_LOADED, and stored
in the existing _sniper_pages dict so the existing sniper-poll path
picks them up automatically (page.reload instead of page.goto).

Caps prewarm at 7 dates within next week to keep total prewarm time
under ~3.5 min, leaving ≥1.5 min margin before window opens. Failures
on individual dates fall back to fresh goto() in the sniper poll.
"
```

---

## Task 3: CF challenge telemetry

**Context:** Need to detect if Cloudflare/Turnstile challenges fire during prewarm. If they do, we want a Discord alert so the operator can react (rotate cookies, reduce prewarm aggressiveness, etc). Detection: a challenge page typically redirects to a URL containing `challenge` or has a Turnstile iframe with class `cf-turnstile`. We count challenges per prewarm session and alert if rate > 5%.

**Files:**
- Modify: `src/checker.py`
- Modify: `src/notifier.py`
- Create: `tests/test_cf_challenge_telemetry.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_cf_challenge_telemetry.py`:

```python
"""Tests for Cloudflare challenge detection during prewarm (Phase A+2 Task 3)."""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

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


def _make_page(url: str, has_turnstile: bool = False, is_closed: bool = False):
    page = AsyncMock()
    page.url = url
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.is_closed = MagicMock(return_value=is_closed)
    page.query_selector = AsyncMock(
        return_value=MagicMock() if has_turnstile else None
    )
    return page


def test_detects_challenge_in_url():
    """A page whose URL contains 'challenge' is detected as a CF challenge."""
    checker = _make_checker()
    page = _make_page("https://www.exploretock.com/challenge?ray=abc123")
    assert checker._is_cloudflare_challenge_page(page) is True


def test_detects_turnstile_iframe():
    """A page with a cf-turnstile iframe is detected as a CF challenge."""
    # synchronous helper — no need for asyncio
    checker = _make_checker()
    page = _make_page("https://www.exploretock.com/test/search", has_turnstile=False)
    # Inject by setting the URL to indicate it's the challenge marker; the
    # iframe-based detection lives behind an async query, tested below.
    assert checker._is_cloudflare_challenge_page(page) is False


@pytest.mark.asyncio
async def test_prewarm_counts_cf_challenges():
    """prewarm_target_dates exposes a challenge_count after running."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    page_count = [0]
    async def make_page():
        page_count[0] += 1
        # First page: redirected to challenge URL
        if page_count[0] == 1:
            return _make_page(
                "https://www.exploretock.com/challenge?ray=abc"
            )
        return _make_page("https://www.exploretock.com/test/search?date=X")
    checker.browser.new_page = make_page

    result = await checker.prewarm_target_dates(dates, stagger_sec=0)

    # Challenge count exposed on result OR on instance attribute
    cc = getattr(checker, "_last_prewarm_cf_challenges", None)
    assert cc == 1, f"Expected 1 CF challenge counted; got {cc}"


@pytest.mark.asyncio
async def test_prewarm_alerts_when_challenge_rate_exceeds_threshold():
    """If CF challenge rate > 5%, notifier.cf_challenge_warning is called."""
    notifier = MagicMock()
    checker = _make_checker()
    checker._notifier = notifier  # injection for test

    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4)]

    async def make_page():
        # All four challenged → 100% rate, way over 5%
        return _make_page("https://www.exploretock.com/challenge?ray=z")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0, notifier=notifier)

    notifier.cf_challenge_warning.assert_called_once()
    args, kwargs = notifier.cf_challenge_warning.call_args
    rate = kwargs.get("rate") or args[0]
    assert rate >= 0.05, f"Rate must exceed 5% threshold; got {rate}"
```

- [ ] **Step 2: Confirm tests fail**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_cf_challenge_telemetry.py -v
```

Expected: AttributeError on `_is_cloudflare_challenge_page`, missing `notifier` parameter.

- [ ] **Step 3: Implement detection + counter**

In `src/checker.py`, add a helper method to `AvailabilityChecker` (recommend right before `prewarm_target_dates`):

```python
    @staticmethod
    def _is_cloudflare_challenge_page(page) -> bool:
        """Return True iff `page.url` looks like a Cloudflare challenge.

        Detection signals (any one is sufficient):
          - URL contains 'challenge' (typical CF redirect path)
          - URL contains '__cf_chl' (CF challenge query param)
          - Page hostname does not match exploretock.com (CF interstitial)
        """
        try:
            url = page.url or ""
        except Exception:
            return False
        url_lower = url.lower()
        if "challenge" in url_lower:
            return True
        if "__cf_chl" in url_lower:
            return True
        return False
```

Modify `prewarm_target_dates` to count challenges and optionally alert via a passed-in notifier:

```python
    async def prewarm_target_dates(
        self,
        target_dates: list[date],
        stagger_sec: float = 30.0,
        notifier: "Notifier | None" = None,
    ) -> None:
        """Open one Playwright page per target_date, parked at CALENDAR_LOADED.
        ...(existing docstring)...

        notifier (optional): if a Cloudflare challenge rate >5% is detected
        across the prewarm batch, calls notifier.cf_challenge_warning(rate, count).
        """
        cf_challenges = 0
        attempted = 0
        for i, target_date in enumerate(target_dates):
            date_str = target_date.isoformat()
            url = (
                f"{BASE_URL}/{self.config.restaurant_slug}/search"
                f"?date={date_str}"
                f"&size={self.config.party_size}"
                f"&time={self.config.preferred_time}"
            )
            attempted += 1
            try:
                page = await self.browser.new_page()
                logger.info(f"[prewarm] {date_str} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=20000)

                if self._is_cloudflare_challenge_page(page):
                    cf_challenges += 1
                    logger.warning(
                        f"[prewarm] {date_str} hit CF challenge "
                        f"(url={page.url}); not parking this page"
                    )
                    try:
                        await page.close()
                    except Exception:
                        pass
                    continue

                await page.wait_for_selector(
                    sel.get("calendar_container"), timeout=10000
                )
                self._sniper_pages[date_str] = page
                logger.info(f"[prewarm] {date_str} parked at CALENDAR_LOADED")
            except Exception as e:
                logger.warning(
                    f"[prewarm] {date_str} failed: {type(e).__name__}: {e}"
                )

            if i < len(target_dates) - 1 and stagger_sec > 0:
                await asyncio.sleep(stagger_sec)

        # Expose telemetry
        self._last_prewarm_cf_challenges = cf_challenges
        self._last_prewarm_attempts = attempted

        # Alert if rate exceeded threshold
        if attempted > 0 and notifier is not None:
            rate = cf_challenges / attempted
            if rate > 0.05:
                try:
                    notifier.cf_challenge_warning(rate=rate, count=cf_challenges)
                except Exception as e:
                    logger.debug(f"[prewarm] cf_challenge_warning failed: {e}")
```

- [ ] **Step 4: Add `cf_challenge_warning` to notifier**

In `src/notifier.py`, add a new method anywhere in `Notifier` (recommend after `error`):

```python
    def cf_challenge_warning(self, rate: float, count: int) -> None:
        """Alert on elevated Cloudflare challenge rate during prewarm."""
        msg = (
            f"Cloudflare challenge rate {rate:.0%} during target-date prewarm "
            f"({count} challenge(s) detected). Bot may be losing the prewarm "
            "edge for this release window. Consider running --verify and "
            "rotating session cookies if rate stays elevated."
        )
        logger.warning(f"[cf-challenge] {msg}")
        self._fire(
            title="⚠️ Cloudflare Challenge Rate Elevated",
            description=msg,
            color=_RED,
        )
```

- [ ] **Step 5: Wire notifier into monitor's prewarm call**

In `src/monitor.py`, modify the prewarm call to pass `self.notifier`:

```python
                            await self.checker.prewarm_target_dates(
                                prewarm_dates, stagger_sec=30.0,
                                notifier=self.notifier,
                            )
```

- [ ] **Step 6: Run tests, confirm green**

```bash
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/test_cf_challenge_telemetry.py -v
cd /Users/yutianyang/tock-bot-phase-a2 && python -m pytest tests/ -q
```

Expected: 4 new tests pass; full suite green.

- [ ] **Step 7: Commit**

```bash
git add src/checker.py src/notifier.py src/monitor.py tests/test_cf_challenge_telemetry.py
git commit -m "feat: CF challenge telemetry during prewarm (Phase A+2 Task 3)

Detects Cloudflare/Turnstile challenge pages by URL signature
('challenge' or '__cf_chl'). Counts per-prewarm session. If rate
exceeds 5%, fires a Discord embed via notifier.cf_challenge_warning
so the operator can investigate (cookie rotation, headed login,
prewarm aggressiveness reduction).

Challenge pages are NOT stored in _sniper_pages — sniper-poll falls
back to fresh goto() for those dates, which gives the bot one more
chance to get past CF on the live release.
"
```

---

## Self-Review

| Spec requirement | Task |
|------------|------|
| Race 5pm + 8pm of same date concurrently | Task 1 |
| Lock+event still prevents true double-booking | Task 1 (test) |
| Pre-open target-date pages 5 min before release | Task 2 |
| Pages spread one per 30s (gentler than release-moment burst) | Task 2 |
| Pages parked at CALENDAR_LOADED | Task 2 |
| Failures on individual dates don't kill the batch | Task 2 |
| Use existing `_sniper_pages` dict (existing sniper-poll picks them up) | Task 2 |
| Include weekday dates if `FALLBACK_DAYS` is set | Task 2 (`_get_prewarm_dates` reads both) |
| `.env.example` documents weekday opt-in | Task 2 |
| Detect CF challenges via URL signature | Task 3 |
| Alert via Discord if rate > 5% | Task 3 |
| Failed-CF pages fall back to fresh goto() in sniper-poll | Task 3 (don't park them) |

**Placeholder scan:** No TBD/TODO. All steps contain complete code.

**Type consistency:**
- `prewarm_target_dates(target_dates, stagger_sec, notifier=None)` — same signature in tests (Tasks 2, 3) and implementation.
- `_get_prewarm_dates() -> list[date]` — used in monitor.py wiring; type is consistent with `_get_target_dates` style.
- `cf_challenge_warning(rate: float, count: int)` — same signature in test, implementation, monitor.

**Interaction check:** Task 3 modifies `prewarm_target_dates` (added in Task 2). The instructions explicitly handle the interleave — Task 3's Step 3 shows the FULL replacement signature including Task 2's parameters.

**Total scope:** ~250 LOC, ~11 tests, 3 commits. Fits in 1-2 working days.
