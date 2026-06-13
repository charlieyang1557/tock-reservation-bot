"""
Booking logic.

Tock checkout flow (after clicking a time slot):
─────────────────────────────────────────────────
  Step 1 — Navigate to search page, click calendar day
  Step 2 — Click the target time slot button
  Step 3 — Checkout page loads (guest details + payment)
            • For FREE reservations: confirm button appears directly
            • For PAID/DEPOSIT reservations: payment section appears first
              - If saved card on file → proceed to confirm
              - If NO card on file   → bot PAUSES and notifies you to add one,
                                       then polls until card appears (up to 10 min)
  Step 4 — Click "Complete reservation" (or equivalent confirm button)
  Step 5 — Confirmation page detected → booking complete

CONCURRENT RACE LOGIC
──────────────────────
When multiple preferred-day slots are available, book_best_slot_race() launches
one asyncio task per calendar date simultaneously.

A shared asyncio.Lock ensures only ONE task can execute the confirm click.
After one task succeeds, it sets a shared asyncio.Event; all other tasks
check this event before the lock and abort immediately.

Because asyncio is single-threaded cooperative multitasking, the event check
inside the lock is effectively atomic — no double-bookings can occur.
"""

import asyncio
import logging
import os
import re
from datetime import datetime
from enum import Enum

from playwright.async_api import Page

import src.selectors as sel
from src import selector_metrics
from src.browser import TockBrowser
from src.checker import AvailableSlot
from src.config import Config
from src.notifier import Notifier
from src.selectors import (
    get_slot_button_selectors,
    is_generic_slot_selector,
    is_playwright_selector,
)

logger = logging.getLogger(__name__)

BASE_URL = "https://www.exploretock.com"


class BookingOutcome(Enum):
    """Result of a booking race.

    CONFIRMED           — slot booked and verified
    UNVERIFIED_CONFIRM  — confirm clicked but verification timed out;
                          Tock MAY have accepted. Operator must verify.
    FAILED              — no confirm clicked, or click verifiably failed
    """
    CONFIRMED = "confirmed"
    UNVERIFIED_CONFIRM = "unverified_confirm"
    FAILED = "failed"

_SCREENSHOT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "debug_screenshots"
)

# Where _dump_click_failure() writes the page HTML when a slot-click fails.
# Always on (failures are rare) so the next release window is self-diagnosing:
# the dump reveals whether the slot vanished (race-loss) or the button was
# present but unmatched (scope bug) — the 2026-06-05 ambiguity.
_BOOKING_FAILURE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "booking_failures"
)
# Cap dump files so a pathological failure burst can't exhaust the disk
# (self-DoS). ~50 full search-page HTMLs ≈ a few tens of MB.
_MAX_FAILURE_DUMPS = 50

# Reload budget for a stale warm page handed to a replay-sourced slot
# (2026-06-09 review fix #2). Bounded tight: the page is already on the
# right ?date= search URL, so this is a same-URL re-render, not a cold goto.
_REPLAY_WARM_RELOAD_TIMEOUT_MS = 10_000

# Per-try checkout wait inside the Book-button click retry loop (Fix 4). Short
# so we can re-click quickly when a click doesn't register; the loop runs up to
# config.slot_click_max_tries times, so the cumulative checkout budget is
# _SLOT_CLICK_CHECKOUT_WAIT_SEC × slot_click_max_tries.
_SLOT_CLICK_CHECKOUT_WAIT_SEC = 2


def _write_failure_dump(
    html: str, clean_url: str, slot_str: str, date_str: str, stage: str
) -> str:
    """Synchronous dump writer (run via asyncio.to_thread). Creates the file
    owner-only (0600) so authenticated page HTML isn't world-readable even when
    the process umask is permissive. Returns the written path."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path = os.path.join(
        _BOOKING_FAILURE_DIR, f"faildom_{stage}_{ts}_{date_str}.html"
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(f"<!-- stage={stage} url={clean_url} slot={slot_str} -->\n")
        f.write(html)
    return path

# Codex MEDIUM 1: classification of "generic Book" selectors (vs.
# specific per-time-slot selectors) now lives in src/selectors.py
# alongside `get_slot_button_selectors` so the two cannot drift apart.
# Use `is_generic_slot_selector(s)` for membership tests.

# Codex LOW 1 + B2 review MEDIUM: match the actual checkout-style URL
# path segment, not the full URL. Substrings in query string or
# fragment (e.g., `/search?next=/checkout/abc`) must NOT match.
_CHECKOUT_PATH_RE = re.compile(
    r"/(checkout|reservation|book)(?:/|$)", re.IGNORECASE
)


def _checkout_url_matches(url: str) -> bool:
    """Return True iff the URL's PATH (not query, not fragment) contains
    a checkout/reservation/book segment. Parses with urllib so query
    strings and fragments can never produce a false positive."""
    if not url:
        return False
    try:
        from urllib.parse import urlparse
        path = urlparse(url).path or ""
    except Exception:
        return False
    return bool(_CHECKOUT_PATH_RE.search(path))


def _slot_time_to_24h(slot_time: "str | None", fallback: str) -> str:
    """Convert a slot time like '8:00 PM' into Tock's URL time= value
    ('20:00'). Falls back to `fallback` (config.preferred_time) when the
    slot time isn't a parseable 'H:MM AM/PM' string — legacy 'Slot N',
    empty, or malformed — so a weird label can never corrupt the URL.

    2026-06-12 fix: the booker previously hardcoded time={preferred_time}
    (17:00) for EVERY slot, so 8:00 PM slots navigated to &time=17:00.
    """
    if not slot_time:
        return fallback
    raw = slot_time.strip().upper()
    # Accept 'H:MM AM/PM', minutes-less 'H AM/PM', their no-space variants
    # ('8:00PM' / '8PM' — the checker's Source-1 span can emit verbatim), and
    # a 24-hour 'HH:MM' label. (codex: '8:00PM'/'8PM'/'20:00' previously fell
    # back to preferred_time → wrong time bucket.)
    for fmt in ("%I:%M %p", "%I %p", "%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(raw, fmt).strftime("%H:%M")
        except (ValueError, AttributeError):
            continue
    # Do NOT fall back silently — a future label-format change that re-creates
    # the 2026-06-12 wrong-time bug must be visible in the log.
    logger.warning(
        f"[book] unparseable slot time {slot_time!r}; seeding search "
        f"time={fallback} (may be wrong — check the slot label format)"
    )
    return fallback


# Codex LOW 2: payment-visible JS includes a URL precondition so
# `/account/payment-methods` (which contains identical "Add card" text)
# cannot false-trigger checkout detection while the bot is on the wrong
# page. The URL check happens INSIDE the page context, not just in
# Python — so the JS sees the live page state at the moment it runs.
_PAYMENT_VISIBLE_JS_SOURCE = (
    "() => {"
    "  const path = (window.location && window.location.pathname || '').toLowerCase();"
    "  if (!/\\/(checkout|reservation|book)(\\/|$)/.test(path)) return false;"
    "  const card = document.querySelector("
    "    '[data-testid=\"saved-card\"], .SavedCard, "
    ".PaymentMethod--saved, [data-testid=\"payment-card\"]'"
    "  );"
    "  if (card) return true;"
    "  const ctrls = Array.from(document.querySelectorAll('button, a'));"
    "  return ctrls.some(el => "
    "    /add (payment|card|a card)/i.test(el.innerText || '')"
    "  );"
    "}"
)


# Single-evaluate slot-click JS (Phase B1.2). Called from _click_time_slot.
# Iterates DOM-side and clicks inside the same call, eliminating per-button
# Python↔browser round-trips. Honors strict_time_match by refusing the
# first-button fallback (Codex HIGH from Phase A+2).
_CLICK_TIME_SLOT_JS = r"""
(args) => {
  const { selector, targetTime, slotTimeRaw, isGeneric, strictTimeMatch,
          cardScopeSelector } = args;
  const buttons = Array.from(document.querySelectorAll(selector));
  const upperTarget = targetTime;
  const escapedSlotTime = slotTimeRaw.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const slotWordRe = new RegExp('\\b' + escapedSlotTime + '\\b', 'i');
  const timeRe = /\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b/i;

  let bestBtn = null;
  let bestText = null;

  for (const btn of buttons) {
    const text = (btn.textContent || btn.innerText || '').trim();
    const upperText = text.toUpperCase();

    if (slotWordRe.test(text)) {
      // WORD-BOUNDARY, not substring: '1:00 PM' must not match '11:00 PM'
      // button text (codex CONFIRMED — same collision as the card branch).
      btn.click();
      return { clicked: true, text, reason: 'exact' };
    }

    const m = text.match(timeRe);
    if (m && m[1].trim().toUpperCase() === upperTarget) {
      btn.click();
      return { clicked: true, text, reason: 'regex' };
    }

    if (isGeneric) {
      // Confirm the time from the ENCLOSING result card, not just the
      // button's immediate parent. Tock's MUI layout (2026-06-12) puts the
      // time label in a sibling subtree of the "Book" button, so a parent-
      // only check missed it and refused to click a live button.
      const ctx = (cardScopeSelector && btn.closest(cardScopeSelector))
        || btn.parentElement;
      const ctxText = ctx
        ? (ctx.textContent || ctx.innerText || '').trim()
        : '';
      // WORD-BOUNDARY match, NOT substring containment: '1:00 PM' is a
      // substring of a sibling card's '11:00 PM' (and '2:00 PM' of '12:00 PM'),
      // which—now that we scope to the whole card—would click the WRONG slot
      // (code-review CONFIRMED). slotWordRe = \b<slotTime>\b, so '1:00 PM' no
      // longer matches '11:00 PM'.
      if (slotWordRe.test(ctxText)) {
        btn.click();
        return {
          clicked: true,
          text: ctxText.slice(0, 80),
          reason: 'generic-card',
        };
      }
      // Generic button without time confirmation in its card — never a fallback
      continue;
    }

    if (bestBtn === null) {
      bestBtn = btn;
      bestText = text;
    }
  }

  if (bestBtn !== null && !strictTimeMatch) {
    bestBtn.click();
    return { clicked: true, text: bestText, reason: 'first-fallback' };
  }
  if (bestBtn !== null && strictTimeMatch) {
    return {
      clicked: false,
      text: bestText,
      reason: 'strict-refused-fallback',
    };
  }
  return { clicked: false, text: null, reason: 'no-match' };
}
"""

# How long to wait for the user to add a payment card (Tock holds slots ~10 min)
PAYMENT_WAIT_TIMEOUT_SEC = 540   # 9 minutes
PAYMENT_POLL_INTERVAL_SEC = 15


class TockBooker:
    def __init__(self, config: Config, browser: TockBrowser, notifier: Notifier):
        self.config = config
        self.browser = browser
        self.notifier = notifier
        # Lock ensures only one concurrent task can execute the confirm step
        self._confirm_lock = asyncio.Lock()
        # Set the moment ANY task is about to click confirm. Other tasks
        # see this and abort even if booking_won never gets set (e.g. when
        # confirm-verification fails but Tock may have accepted the booking).
        # Prevents sequential double-booking from the confirm-uncertainty
        # window identified by Codex's adversarial review of Phase A+2.
        self._confirm_attempted = asyncio.Event()
        # Slot whose confirm click was attempted but could not be verified.
        # Set by _book_single's soft-win path. Read by book_best_slot_race
        # to return UNVERIFIED_CONFIRM. Only a process restart clears this —
        # NOT .clear() at the start of book_best_slot_race like
        # _confirm_attempted. This is the session-level guard that prevents
        # the next monitor poll from starting another race after a soft-win
        # (Codex pass 2 HIGH finding).
        self._unverified_confirm_slot: AvailableSlot | None = None

    # ------------------------------------------------------------------
    # Public: race multiple slots
    # ------------------------------------------------------------------

    async def book_best_slot_race(
        self, slots: list[AvailableSlot],
        warm_pages: dict[str, Page] | None = None,
    ) -> tuple[BookingOutcome, AvailableSlot | None]:
        """
        Race ALL provided slots concurrently — one asyncio task per slot,
        even multiple times on the same calendar date (e.g. Friday 5pm AND
        Friday 8pm). The first task to clear the confirm-lock and succeed
        is the winner; all others see ``booking_won`` set and abort.

        Phase A+2 changed this from "pick best slot per date" to "race all"
        because slots disappear within ~5s of release on competitive
        restaurants — racing both 5pm and 8pm of the same Friday roughly
        doubles the hit chance.

        Warm pages are claimed via pop(), so each warm page is owned by
        exactly one task. Additional same-date tasks open fresh pages.

        Returns a (BookingOutcome, slot_or_none) tuple:
          CONFIRMED          — slot booked and verified; slot is the winner
          UNVERIFIED_CONFIRM — confirm clicked but unverifiable; slot is the
                               attempted slot. Bot must idle until restart.
          FAILED             — all attempts failed; slot is None.
        """
        if not slots:
            return BookingOutcome.FAILED, None

        # Persistent uncertain-booking guard. Survives auto-restart, Mac mini
        # reboots, PM2 restarts. Operator must remove booking_uncertain.json
        # after verifying on Tock. Codex pass 3 found that the in-memory
        # _unverified_confirm_slot was erased by main.py's auto-restart loop
        # (constructs a fresh TockBooker), enabling silent double-booking.
        from src.booking_uncertain import read_uncertain
        persisted = read_uncertain()
        if persisted is not None:
            logger.warning(
                f"[book] Refusing to start race — booking_uncertain.json "
                f"records an unverifiable confirm for "
                f"{persisted.slot_date_str} ({persisted.day_of_week}) @ "
                f"{persisted.slot_time}. Verify at "
                "https://www.exploretock.com/account/reservations and "
                "remove the file before running again."
            )
            from src.checker import AvailableSlot as _AvailableSlot
            from datetime import date as _date_cls
            try:
                slot_date = _date_cls.fromisoformat(persisted.slot_date_str)
            except ValueError:
                slot_date = _date_cls.today()  # corrupt; degrade gracefully
            return (
                BookingOutcome.UNVERIFIED_CONFIRM,
                _AvailableSlot(
                    slot_date=slot_date,
                    slot_time=persisted.slot_time,
                    day_of_week=persisted.day_of_week,
                ),
            )

        # Fall through: also check the in-memory guard (race within same
        # process where the file write may have failed).
        if self._unverified_confirm_slot is not None:
            logger.warning(
                f"[book] Refusing to start race — previous race in this "
                f"process had unverified confirm for {self._unverified_confirm_slot}."
            )
            return BookingOutcome.UNVERIFIED_CONFIRM, self._unverified_confirm_slot

        # Reset the confirm-attempted gate for this race only. The
        # _unverified_confirm_slot above persists across races within
        # the same process — only a restart clears it.
        self._confirm_attempted.clear()

        # Phase A+2: race ALL slots, not just one per date. The asyncio.Lock +
        # booking_won.Event serialize the actual confirm click — so attempting
        # 5pm AND 8pm of the same Friday concurrently still produces at most
        # one booking. Maximizes hit rate when releases drop multiple times
        # on the same date.
        candidates = list(slots)  # shallow copy so caller mutations during the race are isolated
        logger.info(
            f"Starting concurrent booking race for {len(candidates)} slot(s): "
            + " | ".join(str(s) for s in candidates)
        )

        booking_won = asyncio.Event()
        winner: list[AvailableSlot] = []

        async def attempt(slot: AvailableSlot) -> None:
            self.notifier.booking_attempting(slot)
            # Use pop() so each warm page is claimed by exactly ONE task. After
            # Phase A+2's race-all-slots change, multiple slots on the same date
            # can race concurrently — without pop(), both tasks would grab the
            # same Page object and corrupt each other's DOM state. Subsequent
            # tasks for the same date fall through to new_page() (slower but
            # state-isolated; only the loser pays the cost).
            page = warm_pages.pop(slot.slot_date_str, None) if warm_pages else None
            try:
                success = await self._book_single(slot, booking_won, warm_page=page)
                if success:
                    winner.append(slot)
            except Exception as e:
                logger.error(f"[book] Unhandled exception for {slot}: {e}")

        tasks = [asyncio.create_task(attempt(s)) for s in candidates]
        await asyncio.gather(*tasks, return_exceptions=True)

        if winner:
            return BookingOutcome.CONFIRMED, winner[0]
        if self._unverified_confirm_slot is not None:
            return BookingOutcome.UNVERIFIED_CONFIRM, self._unverified_confirm_slot
        return BookingOutcome.FAILED, None

    # ------------------------------------------------------------------
    # Internal: book one slot
    # ------------------------------------------------------------------

    async def _book_single(
        self, slot: AvailableSlot, booking_won: asyncio.Event,
        warm_page: Page | None = None,
    ) -> bool:
        """
        Full booking flow for one slot on its own Playwright page.
        Returns True if the booking was confirmed.

        If warm_page is provided (sniper mode), skips Steps 1-2 (navigation +
        day click) and jumps straight to clicking the time slot — saving ~3-5s.
        """
        if self.config.dry_run:
            self.notifier.dry_run_would_book(slot)
            return False

        # Use warm page from checker (sniper warm or normal-mode handoff) or
        # create fresh. A page is considered usable only if it's still open;
        # a closed handoff is logged so operators can distinguish between
        # "no warm page provided" and "provided but stale".
        warm_page_unusable = warm_page is not None and warm_page.is_closed()
        if warm_page_unusable:
            logger.warning(
                f"[book] {slot} — warm page provided but already closed; "
                "falling back to fresh navigation"
            )
        page = warm_page if warm_page and not warm_page.is_closed() else None
        owns_page = page is None  # True ⇒ booker created a fresh page; False ⇒ using warm
        # Phase B3.3: when we need a fresh page, prefer the pre-warmed pool
        # so we don't pay full new_page() launch cost on cold race tasks.
        # Falls through to new_page() when the pool is empty.
        # Use isinstance(PagePool) — not getattr(...) is not None — because
        # tests construct browser via MagicMock() where every attribute
        # auto-resolves to another MagicMock. PagePool is the only valid
        # source of pre-warmed pages, so isinstance is the safe sentinel.
        from src.page_pool import PagePool  # local import: avoids cycle
        from_pool = False
        if page is None:
            page_pool = getattr(self.browser, "page_pool", None)
            if isinstance(page_pool, PagePool):
                page = await page_pool.acquire()
                from_pool = True
            else:
                page = await self.browser.new_page()

        try:
            if owns_page:
                # ── Step 1: load search page ──────────────────────────────
                url = self._build_search_url(slot)
                logger.info(f"[book] {slot} → {url}")
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)

                if not await self._wait_for_selector(
                    page, "calendar_container", context=str(slot), timeout=15000
                ):
                    return False

                # B1.5: when skip_day_click_check is True, defer the calendar-day
                # click until after we try the time-slot click — Tock's
                # `?date=YYYY-MM-DD` URL may already select the date in the
                # SPA so the click is redundant. If the time-slot click finds
                # no buttons, we fall back to clicking the day and retrying
                # once.
                skip_click = self.config.skip_day_click_check
                day_clicked = False
                if not skip_click:
                    # Wait for day buttons to render inside the calendar.
                    try:
                        await page.wait_for_selector(
                            sel.get("all_day_button"), timeout=5000
                        )
                    except Exception:
                        pass  # calendar may still be loading; proceed anyway

                    # ── Step 2: click the calendar day ────────────────────
                    if booking_won.is_set():
                        self.notifier.booking_aborted(slot, "another slot already booked")
                        return False

                    if not await self._click_calendar_day(page, slot):
                        return False
                    day_clicked = True
                    # No explicit slot-button wait here — _click_time_slot
                    # owns it (a duplicate stacks dead time when buttons
                    # never render).
            else:
                if slot.source == "replay":
                    # 2026-06-09 review fix #2 (hardened by merge review): a
                    # replay-detected slot was decoded from the protobuf body
                    # — NO page has rendered the release. This warm page was
                    # last rendered by an EARLIER poll (the replay fast path
                    # skips the DOM scan), so its DOM still shows pre-release
                    # content with no Book button; clicking it as-is repeats
                    # the 06/05 detected-but-click-failed incident.
                    if booking_won.is_set():
                        # Don't pay the ~10s reload for a race already lost.
                        self.notifier.booking_aborted(
                            slot, "another slot already booked"
                        )
                        return False
                    # Navigate to the slot's OWN search URL (time= seeded from
                    # the slot, Fix 3) instead of reload()-ing the parked URL:
                    # warm/prewarm pages are parked at time=preferred_time, which
                    # renders the wrong time bucket for a non-preferred slot
                    # (e.g. an 8 PM replay slot on a 17:00-parked page) and makes
                    # strict matching miss it (codex HIGH). Still a warm-session
                    # navigation (cookies/connection hot), just the correct URL.
                    warm_url = self._build_search_url(slot)
                    logger.info(
                        f"[book] {slot} → replay-sourced slot on warm page; "
                        f"navigating to render post-release DOM → {warm_url}"
                    )
                    try:
                        await page.goto(
                            warm_url,
                            wait_until="domcontentloaded",
                            timeout=_REPLAY_WARM_RELOAD_TIMEOUT_MS,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[book] {slot} — warm-page reload failed "
                            f"({type(e).__name__}: {e})"
                        )
                        if page.is_closed():
                            # Page died mid-reload (CF kill, renderer crash):
                            # fail fast — grinding the click pipeline against
                            # a dead page burns the race attempt for nothing;
                            # sibling tasks / the next poll recover.
                            logger.error(
                                f"[book] {slot} — warm page died during "
                                "reload; abandoning attempt"
                            )
                            return False
                        logger.info(
                            f"[book] {slot} — page still open; attempting "
                            "click on current DOM"
                        )
                    # domcontentloaded fires BEFORE SPA hydration. Give the
                    # calendar the same render budget the checker gives its
                    # own reuse-poll reloads (10s) — without this, the only
                    # wait between reload and the strict click was the slot-
                    # button race, far short of release-night render times.
                    if not await self._wait_for_selector(
                        page, "calendar_container", context=str(slot),
                        timeout=10000,
                    ):
                        return False
                    # Trust the page's own ?date= URL for day selection
                    # (the reload re-navigates the same per-date search URL
                    # this page was parked on) and ARM the day-click-and-
                    # retry fallback below instead of eagerly clicking the
                    # day: _click_calendar_day matches day-NUMBER text with
                    # no month validation, so an eager click is itself a
                    # wrong-date vector on month boundaries — and can
                    # de-select the date the URL already selected.
                    skip_click = True
                    day_clicked = False
                else:
                    # DOM-found slot: the checker already had the correct
                    # date selected on this page. Initialize so the
                    # post-Step-3 fallback below never trips.
                    skip_click = False
                    day_clicked = True
                logger.info(f"[book] {slot} → using warm page (skipping navigation)")

            await self._booking_screenshot(page, "01_booking_start")

            # ── Step 3+4: click the Book button (retry) → checkout ─────
            # A single click can fail to register even on a live button, so we
            # re-click up to config.slot_click_max_tries times, waiting a short
            # window for checkout each try (Fix 4, 2026-06-13). Aborts on
            # booking_won / closed page; dumps the DOM on exhaustion.
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            checkout_ok = await self._click_until_checkout(
                page, slot, owns_page=owns_page, skip_click=skip_click,
                day_clicked=day_clicked, booking_won=booking_won,
            )
            await self._booking_screenshot(
                page,
                "03_checkout_loaded" if checkout_ok else "03_checkout_timeout"
            )
            if not checkout_ok:
                # _click_until_checkout already dumped the DOM on exhaustion.
                return False

            # ── Step 5a: prep (NO lock — concurrent across tasks) ─────
            # Phase B3.1: payment detection, CVC fill, and wait-for-confirm-
            # button used to all run under the lock, serializing every
            # racing task. They're idempotent per-page, so we run them
            # without the lock to maximize concurrency.
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            prep_ok = await self._prepare_for_confirm(
                page, slot, booking_won=booking_won
            )
            if not prep_ok:
                if not booking_won.is_set():
                    await self._dump_click_failure(page, slot, stage="prep")
                return False

            # ── Step 5b: confirm click + verify (locked — only one wins) ─
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False

            async with self._confirm_lock:
                # Re-check inside the lock (another task may have won while
                # we were waiting to acquire it)
                if booking_won.is_set():
                    self.notifier.booking_aborted(
                        slot, "another slot confirmed while waiting for lock"
                    )
                    return False
                if self._confirm_attempted.is_set():
                    # Another task already clicked confirm but couldn't verify.
                    # Tock may have accepted that booking — do NOT click again.
                    self.notifier.booking_aborted(
                        slot,
                        "another slot has an unverified confirm in flight — "
                        "skipping to avoid double-booking",
                    )
                    return False

                # Mark that we are about to click confirm. From this point on,
                # no other task can click even if our own verification fails
                # — the user must verify manually.
                self._confirm_attempted.set()

                success = await self._execute_confirm_click_and_verify(page, slot)
                if success:
                    booking_won.set()
                    self.notifier.booking_confirmed(slot)
                    return True

                # Click happened but verification failed. Tock may have
                # accepted the booking silently. Set BOTH the in-memory flag
                # AND write the disk file. The disk file survives main.py's
                # auto-restart loop (Codex pass 3 finding); the in-memory
                # flag covers the same-process case where disk write fails.
                self._unverified_confirm_slot = slot
                from src.booking_uncertain import (
                    UncertainBooking, write_uncertain
                )
                from datetime import datetime as _dt
                write_uncertain(UncertainBooking(
                    slot_date_str=slot.slot_date_str,
                    slot_time=slot.slot_time,
                    day_of_week=slot.day_of_week,
                    detected_at_iso=_dt.now().isoformat(),
                ))
                logger.error(
                    f"[book] Confirm clicked for {slot} but verification "
                    "failed. Possible silent success — operator must verify "
                    "AND remove booking_uncertain.json before next run."
                )
                self.notifier.error(
                    "⚠️ Booking confirmation unverifiable",
                    f"Clicked confirm for {slot} but could not verify success. "
                    "Tock MAY have accepted the booking. "
                    "1) Check https://www.exploretock.com/account/reservations "
                    "2) If no reservation: rm booking_uncertain.json then restart "
                    "3) If reservation present: keep the file in place; the bot "
                    "will refuse all future booking attempts. "
                    "This guard now SURVIVES restart (Codex pass 3 fix).",
                )
                booking_won.set()
                return False

        except Exception as e:
            logger.error(f"[book] Error booking {slot}: {e}")
            return False
        finally:
            # After Phase A+2: warm pages are popped from checker ownership
            # via pop_warm_page() and transferred to this method. The booker
            # is now responsible for closing them after every attempt — success
            # OR failure — to prevent the leak Codex pass 3 caught.
            #
            # Phase B3.3: pages obtained from the page pool go through
            # pool.release() (which closes them — DOM is dirty after a race
            # attempt). All other booker-owned pages still close directly.
            if page is not None:
                try:
                    if from_pool:
                        await self.browser.page_pool.release(page)
                    elif not page.is_closed():
                        await page.close()
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Step helpers
    # ------------------------------------------------------------------

    def _build_search_url(self, slot: AvailableSlot) -> str:
        """Per-date Tock search URL for a fresh-navigation booking.

        time= is seeded from the slot's OWN time (via _slot_time_to_24h),
        not config.preferred_time — so an 8:00 PM slot navigates to
        &time=20:00 instead of the old hardcoded &time=17:00 (2026-06-12).
        """
        time_param = _slot_time_to_24h(slot.slot_time, self.config.preferred_time)
        return (
            f"{BASE_URL}/{self.config.restaurant_slug}/search"
            f"?date={slot.slot_date_str}"
            f"&size={self.config.party_size}"
            f"&time={time_param}"
        )

    async def _attempt_slot_click(
        self, page: Page, slot: AvailableSlot, *,
        using_warm_page: bool, skip_click: bool, day_clicked: bool,
        allow_tagged: bool, booking_won: "asyncio.Event | None" = None,
    ) -> "tuple[bool, bool]":
        """One Book-button click attempt. Returns (clicked, day_clicked).

        Encapsulates the tagged fast-path (warm page only, first try),
        strict/loose time match, and the skip-mode calendar-day fallback so the
        retry loop can re-run it cleanly. `clicked` reflects whether a slot
        button was found and clicked this try; the loop relies on checkout
        detection (not this flag) for ultimate success.
        """
        if using_warm_page and slot.target_selector and allow_tagged:
            if await self._click_tagged_slot(page, slot):
                return True, day_clicked
            logger.info(
                f"[book] {slot} — tagged element not found; rescanning live "
                "DOM (strict)"
            )
        clicked = await self._click_time_slot(
            page, slot, strict_time_match=using_warm_page
        )
        if not clicked and skip_click and not day_clicked:
            # B1.5: skip-mode found no slot buttons — click the calendar day
            # (once) and retry the time-slot click. Don't start this fresh
            # day-click + re-click if another slot already won — it would
            # place a redundant hold (codex: restore the guard dropped in the
            # Fix-4 refactor).
            if booking_won is not None and booking_won.is_set():
                return False, day_clicked
            if await self._click_calendar_day(page, slot):
                day_clicked = True
                clicked = await self._click_time_slot(
                    page, slot, strict_time_match=using_warm_page
                )
        return clicked, day_clicked

    async def _click_until_checkout(
        self, page: Page, slot: AvailableSlot, *,
        owns_page: bool, skip_click: bool, day_clicked: bool,
        booking_won: asyncio.Event,
    ) -> bool:
        """Click the slot's Book button and wait for checkout, RETRYING the
        click up to config.slot_click_max_tries times (Fix 4, 2026-06-13).

        A single click can fail to register even on a live button; a later
        click often goes through, so we don't give up after one. Each try
        re-clicks and waits a SHORT window (_SLOT_CLICK_CHECKOUT_WAIT_SEC) for
        checkout — a prior click that's slowly navigating is still caught by a
        later try's wait. Breaks as soon as checkout loads. Aborts early if
        another slot already won or the page closed. On exhaustion, dumps the
        DOM (stage='checkout' if we ever clicked, else 'click') and returns
        False.
        """
        max_tries = max(1, getattr(self.config, "slot_click_max_tries", 10))
        using_warm_page = not owns_page
        ever_clicked = False
        reloaded_recovery = False
        for click_try in range(1, max_tries + 1):
            if booking_won.is_set():
                self.notifier.booking_aborted(slot, "another slot already booked")
                return False
            if page.is_closed():
                logger.warning(
                    f"[book] {slot} — page closed mid click-retry "
                    f"(try {click_try}/{max_tries}); abandoning"
                )
                return False

            # If a PRIOR click already navigated toward checkout, do NOT
            # re-click the Book button — a second slot-click risks a duplicate
            # hold or bouncing the in-flight navigation. Just confirm checkout.
            on_checkout = False
            if click_try > 1:
                try:
                    on_checkout = _checkout_url_matches(page.url)
                except Exception:
                    on_checkout = False

            if on_checkout:
                clicked = False
            else:
                clicked, day_clicked = await self._attempt_slot_click(
                    page, slot, using_warm_page=using_warm_page,
                    skip_click=skip_click, day_clicked=day_clicked,
                    allow_tagged=(click_try == 1), booking_won=booking_won,
                )
                ever_clicked = ever_clicked or clicked

            # Stale-warm-page recovery (drain residual): a handed-over warm page
            # can be a STALE pre-release prewarm/handoff DOM with no Book button,
            # and for a non-replay slot the booker didn't reload it. If we're on
            # a warm page and NO button has been found at all, reload ONCE to the
            # slot's URL and retry — rather than burning every try on a dead page.
            if (using_warm_page and not reloaded_recovery and not clicked
                    and not ever_clicked and not on_checkout
                    and click_try < max_tries
                    and not booking_won.is_set() and not page.is_closed()):
                reloaded_recovery = True
                recover_url = self._build_search_url(slot)
                logger.info(
                    f"[book] {slot} — warm page has no slot button; reloading to "
                    f"slot URL and retrying (stale-page recovery) → {recover_url}"
                )
                try:
                    await page.goto(
                        recover_url, wait_until="domcontentloaded",
                        timeout=_REPLAY_WARM_RELOAD_TIMEOUT_MS,
                    )
                    await self._wait_for_selector(
                        page, "calendar_container", context=str(slot),
                        timeout=10000,
                    )
                except Exception as e:
                    logger.warning(
                        f"[book] {slot} — stale-page reload recovery failed "
                        f"({type(e).__name__}: {e})"
                    )
                # Re-arm the calendar-day fallback on the FRESH DOM: the goto
                # reloaded the search page, so the day is no longer selected.
                # Mirror the replay path (skip_click=True, day_clicked=False) so
                # _attempt_slot_click can re-click the day if the ?date= URL
                # alone doesn't select it (code-review CONFIRMED).
                skip_click = True
                day_clicked = False
                continue  # retry the click on the freshly-loaded page

            # Wait for checkout if a Book button was clicked this try or a
            # previous one, or we're already navigating to checkout — a click
            # may be in flight. If nothing has ever been clicked (no button
            # found), there's nothing to load, so skip the checkout budget and
            # just re-find the button next try.
            if clicked or ever_clicked or on_checkout:
                # Scroll so a below-the-fold confirm button is reachable once
                # checkout renders (best-effort).
                try:
                    await page.evaluate(
                        "window.scrollTo(0, document.body.scrollHeight)"
                    )
                except Exception:
                    pass

                if await self._wait_for_checkout(
                    page, slot, booking_won=booking_won,
                    timeout_sec=_SLOT_CLICK_CHECKOUT_WAIT_SEC,
                ):
                    if click_try > 1:
                        logger.info(
                            f"[book] {slot} — checkout reached on click try "
                            f"{click_try}/{max_tries}"
                        )
                    return True

            if click_try < max_tries:
                logger.info(
                    f"[book] {slot} — click {click_try}/{max_tries} did not "
                    "reach checkout; re-clicking the Book button"
                )

        # The retry loop used SHORT (per-try) checkout waits so it could
        # re-click quickly. If we DID click but checkout never appeared in
        # those short windows, give the (possibly slow) navigation one FINAL
        # full-budget wait before giving up — so the click-retry budget never
        # shrinks the checkout patience and abandons an already-won hold
        # (codex HIGH).
        if ever_clicked and not booking_won.is_set() and not page.is_closed():
            if await self._wait_for_checkout(page, slot, booking_won=booking_won):
                logger.info(f"[book] {slot} — checkout reached on final wait")
                return True
        if not booking_won.is_set():
            stage = "checkout" if ever_clicked else "click"
            await self._dump_click_failure(page, slot, stage=stage)
        return False

    async def _click_calendar_day(self, page: Page, slot: AvailableSlot) -> bool:
        """Click the calendar button matching slot.slot_date using single evaluate().

        Uses all_day_button (any in-month day) — NOT available_day_button —
        so we click days even when they lack the is-available class (e.g.
        Fuhuihua shows is-sold/is-disabled until the exact release moment).
        """
        selector = sel.get("all_day_button")
        target_num = str(slot.slot_date.day)

        result = await page.evaluate("""
        ([selector, targetNum]) => {
            const buttons = document.querySelectorAll(selector);
            for (const btn of buttons) {
                if (btn.textContent.trim() === targetNum) {
                    btn.click();
                    return true;
                }
            }
            return false;
        }
        """, [selector, target_num])

        if result:
            logger.info(f"[book] Clicked day {target_num} for {slot.slot_date_str}")
            return True

        logger.error(
            f"SELECTOR_FAILED: key='all_day_button'\n"
            f"  Could not find or click day {target_num} for {slot.slot_date_str}.\n"
            f"  -> Update src/selectors.py"
        )
        return False

    async def _wait_any_slot_button(
        self, page: Page, timeout_ms: int = 4000
    ) -> bool:
        """Wait until ANY selector in the slot-button cascade appears.

        Gap-3 fix (2026-06-10): replaces the sequential wait over
        slot_selectors[:2] (2s × 2). On the 2026-06-05 release the live
        DOM matched ONLY the generic Book selector — 3rd in the cascade —
        so the sequential loop burned the full 4s (the 20:00:09→20:00:13
        gap in the incident log) before the cascade scan instantly found
        the buttons. All selectors race concurrently under ONE shared
        budget; returns True as soon as one resolves, False if none do.

        The 4000ms default keeps the OLD worst-case render allowance
        (2s × 2) — buttons appearing at t≈3s under release-night load were
        caught before and must stay caught; a match still returns
        immediately. Note: cancelling a loser does not stop the browser-
        side poller — it self-terminates at timeout_ms.
        """
        tasks = [
            asyncio.create_task(page.wait_for_selector(s, timeout=timeout_ms))
            for s in get_slot_button_selectors()
        ]
        found = False
        pending = set(tasks)
        try:
            while pending and not found:
                done, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                # Materialize ALL verdicts before any() — a short-circuited
                # generator would leave same-batch losers' exceptions
                # un-retrieved → "Task exception was never retrieved" spam
                # in bot.log mid-race.
                verdicts = [
                    not t.cancelled() and t.exception() is None for t in done
                ]
                found = any(verdicts)
        finally:
            for t in pending:
                t.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        return found

    async def _click_time_slot(
        self, page: Page, slot: AvailableSlot,
        strict_time_match: bool = False,
    ) -> bool:
        """Find the time slot matching slot.slot_time and click it.

        Iterates all matching buttons and compares text content to find the
        correct time.

        strict_time_match=False (default): if no exact match is found, falls
          back to clicking the first non-generic button. Acceptable for fresh
          navigation where the page state was just established and a missing
          exact time often means the button text format changed slightly.

        strict_time_match=True: refuse the fallback. Used when the booker
          is operating on a warm/handoff page that was last touched by the
          checker — if the target time isn't visible now, the slot likely
          vanished and clicking any other button would book the wrong time
          (Codex adversarial review HIGH finding).
        """
        slot_selectors = get_slot_button_selectors()

        # Wait reactively for slot buttons — all selectors race at once
        # (gap-3 fix: the old [:2] sequential wait cost 4s on the 06/05 DOM)
        await self._wait_any_slot_button(page)

        # Find which selector has buttons
        matched_selector = None
        for try_sel in slot_selectors:
            try:
                count = await page.locator(try_sel).count()
                if count > 0:
                    matched_selector = try_sel
                    logger.debug(f"[book] Found {count} slot button(s) via {try_sel!r}")
                    break
            except Exception:
                continue

        if not matched_selector:
            logger.error(
                "[book] No slot buttons found after clicking the day.\n"
                "  Tried all known selectors.\n"
                "  -> Update src/selectors.py"
            )
            return False

        # B2.4: telemetry for selector hit-rate analysis. Best-effort —
        # never let a metrics bug break the booking flow.
        try:
            selector_metrics.record_match("slot_button_book", matched_selector)
        except Exception as exc:
            logger.debug(f"[book] selector_metrics.record_match failed: {exc}")

        # Single browser-side iteration: match + click in one round-trip.
        target_time = slot.slot_time.strip().upper()
        is_generic = is_generic_slot_selector(matched_selector)

        # Codex HIGH 2: Playwright-only selectors (`:has-text`, `:text`,
        # `:visible`) cannot be passed to document.querySelectorAll —
        # fall back to a Python locator iteration for those, preserving
        # the JS fast path for plain CSS selectors.
        if is_playwright_selector(matched_selector):
            return await self._click_time_slot_locator_loop(
                page, slot, matched_selector, is_generic, strict_time_match
            )

        try:
            result = await page.evaluate(
                _CLICK_TIME_SLOT_JS,
                {
                    "selector": matched_selector,
                    "targetTime": target_time,
                    "slotTimeRaw": slot.slot_time,
                    "isGeneric": is_generic,
                    "strictTimeMatch": strict_time_match,
                    "cardScopeSelector": sel.RESULT_CARD_SCOPE_SELECTOR,
                },
            )
        except Exception as e:
            logger.error(f"[book] Slot-click evaluate failed: {type(e).__name__}: {e}")
            return False

        if not isinstance(result, dict):
            logger.error(f"[book] Slot-click JS returned unexpected shape: {result!r}")
            return False

        clicked = bool(result.get("clicked"))
        reason = result.get("reason", "")
        text = result.get("text")

        if clicked:
            if reason == "exact":
                logger.info(
                    f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                )
            elif reason == "regex":
                logger.info(f"[book] Clicked slot button (regex match): {text}")
            elif reason in ("generic-card", "generic-parent"):
                logger.info(
                    f"[book] Clicked generic 'Book' button — "
                    f"time confirmed in result card: {text!r}"
                )
            elif reason == "first-fallback":
                logger.warning(
                    f"[book] No exact time match for '{slot.slot_time}' — "
                    f"clicked first specific button: {text}"
                )
            else:
                logger.info(
                    f"[book] Clicked slot button (reason={reason!r}): {text}"
                )
            return True

        if reason == "strict-refused-fallback":
            logger.warning(
                f"[book] No exact time match for '{slot.slot_time}' on warm "
                "page; refusing first-button fallback (slot likely vanished) "
                "— returning False so the race tries another slot or a fresh "
                "page next poll"
            )
        else:
            logger.error(
                f"[book] No clickable slot button found for '{slot.slot_time}' "
                f"(selector: {matched_selector!r})"
            )
        return False

    async def _card_scope_text(self, btn) -> str:
        """Text of the result card enclosing a generic 'Book' button.

        Falls back to the button's immediate parent when no card ancestor
        matches. Lets the matcher confirm the slot time even when it lives
        in a sibling subtree of the button (Tock's MUI search-result layout,
        2026-06-12) — the gap that made the old btn.locator('..') check miss.
        """
        try:
            text = await btn.evaluate(
                """(el, sel) => {
                    const ctx = (sel && el.closest(sel)) || el.parentElement;
                    return ctx ? (ctx.textContent || ctx.innerText || '') : '';
                }""",
                sel.RESULT_CARD_SCOPE_SELECTOR,
            )
        except Exception:
            return ""
        return (text or "").strip()

    async def _click_time_slot_locator_loop(
        self, page: Page, slot: AvailableSlot,
        matched_selector: str, is_generic: bool, strict_time_match: bool,
    ) -> bool:
        """Locator-based fallback for Playwright-only matched selectors
        (`:has-text`, `:text(`, `:visible`) which can't be passed to
        document.querySelectorAll. Mirrors the pre-B1.2 algorithm but
        runs on the slow path only when the JS fast path is unsafe.
        """
        target_time = slot.slot_time.strip().upper()
        slot_word_re = re.compile(
            r"\b" + re.escape(slot.slot_time) + r"\b", re.IGNORECASE
        )
        time_re = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.IGNORECASE)

        locator = page.locator(matched_selector)
        try:
            count = await locator.count()
        except Exception as e:
            logger.error(f"[book] PW-locator count failed: {type(e).__name__}: {e}")
            return False

        best_btn = None
        for i in range(count):
            btn = locator.nth(i)
            try:
                text = (await btn.text_content() or "").strip()
                # WORD-BOUNDARY match on the button's own text, not substring:
                # '1:00 PM' must not match an '11:00 PM' button (codex CONFIRMED).
                if slot_word_re.search(text):
                    await btn.click()
                    logger.info(
                        f"[book] Clicked slot button matching '{slot.slot_time}': {text}"
                    )
                    return True
                # Regex time match
                m = time_re.search(text)
                if m and m.group(1).strip().upper() == target_time:
                    await btn.click()
                    logger.info(f"[book] Clicked slot button (regex match): {text}")
                    return True
                # Generic 'Book' button: confirm the time from the ENCLOSING
                # result card. Tock's MUI layout (2026-06-12) puts the time
                # label in a sibling subtree of the button, so the old
                # immediate-parent check missed it and refused a live button.
                if is_generic:
                    ctx_text = await self._card_scope_text(btn)
                    # WORD-BOUNDARY match, not substring containment (see
                    # _CLICK_TIME_SLOT_JS): '1:00 PM' must NOT match a sibling
                    # card's '11:00 PM' now that we scope to the whole card.
                    if slot_word_re.search(ctx_text):
                        await btn.click()
                        logger.info(
                            f"[book] Clicked generic 'Book' button — "
                            f"time confirmed in result card: {ctx_text[:80]!r}"
                        )
                        return True
                    continue  # never set best_btn for unmatched generic
                if best_btn is None:
                    best_btn = btn
            except Exception:
                continue

        if best_btn is not None and not strict_time_match:
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
        if best_btn is not None and strict_time_match:
            logger.warning(
                f"[book] No exact time match for '{slot.slot_time}' on warm "
                "page; refusing first-button fallback (slot likely vanished) "
                "— returning False so the race tries another slot or a fresh "
                "page next poll"
            )
        logger.error(
            f"[book] No clickable slot button found for '{slot.slot_time}' "
            f"(selector: {matched_selector!r})"
        )
        return False

    async def _click_tagged_slot(self, page: Page, slot: AvailableSlot) -> bool:
        """Click the exact button the checker tagged with data-sniper-target.

        Returns True if the tagged element was found and clicked. Returns False
        when the slot carries no tag (fresh-nav booking) or the tag is no longer
        in the DOM (the slot vanished between detection and this click) — the
        caller treats a False on a warm page as a vanished slot.
        """
        target = getattr(slot, "target_selector", None)
        if not target:
            return False
        try:
            loc = page.locator(target)
            if await loc.count() > 0:
                # Bounded timeout: if the tagged button is present-but-not-yet
                # actionable, fail fast (3s) and let the caller rescan, rather
                # than inheriting Playwright's 30s default on the race path.
                # no_wait_after=True: don't fold the post-click checkout
                # navigation into the click budget — _wait_for_checkout owns
                # that — so a successful click can't time out as a false miss.
                await loc.first.click(timeout=3000, no_wait_after=True)
                logger.info(
                    f"[book] Clicked checker-tagged slot {slot} via {target}"
                )
                return True
        except Exception as e:
            logger.error(
                f"[book] Tagged-slot click failed for {slot} "
                f"({target}): {type(e).__name__}: {e}"
            )
        return False

    async def _dump_click_failure(
        self, page: Page, slot: AvailableSlot, stage: str = "click"
    ) -> None:
        """Dump the live page HTML on a booking-stage failure so the next
        release window is self-diagnosing. `stage` (click/checkout/prep) is
        reflected in the filename and log line so the artifact is NOT mislabeled
        as a click-match failure when the click actually succeeded. Best-effort.
        """
        # Cap check FIRST (cheap listdir) so we don't pay for a full-page
        # serialization just to discard it once the cap is reached.
        try:
            os.makedirs(_BOOKING_FAILURE_DIR, mode=0o700, exist_ok=True)
            try:
                os.chmod(_BOOKING_FAILURE_DIR, 0o700)  # tighten a pre-existing dir
            except OSError:
                pass
            existing = [
                f for f in os.listdir(_BOOKING_FAILURE_DIR)
                if f.startswith("faildom_")
            ]
        except OSError:
            existing = []
        if len(existing) >= _MAX_FAILURE_DUMPS:
            logger.debug(
                f"[book] {stage} failure DOM dump skipped — "
                f"{_MAX_FAILURE_DUMPS}-file cap reached in {_BOOKING_FAILURE_DIR}"
            )
            return

        try:
            html = await page.content()
        except Exception as e:
            logger.debug(f"[book] {stage} failure dump skipped (no content): {e}")
            return

        # Strip query/fragment from the URL header — they carry session-ish
        # params not needed for a diagnostic artifact.
        raw_url = getattr(page, "url", "") or ""
        try:
            from urllib.parse import urlparse, urlunparse
            clean_url = urlunparse(urlparse(raw_url)._replace(query="", fragment=""))
        except Exception:
            clean_url = ""

        # Offload the blocking file write to a thread so a large HTML dump can't
        # stall the event loop (and another racing task's confirm-verify).
        try:
            path = await asyncio.to_thread(
                _write_failure_dump, html, clean_url, str(slot),
                slot.slot_date_str, stage,
            )
            logger.error(f"[book] {stage}-stage failure DOM dumped → {path}")
        except Exception as e:
            logger.debug(f"[book] {stage} failure DOM dump failed: {e}")

    async def _wait_for_checkout(
        self, page: Page, slot: AvailableSlot,
        booking_won: asyncio.Event | None = None,
        timeout_sec: int = 30,
    ) -> bool:
        """Return True when the checkout/booking-details page is detected.

        When `booking_won` is provided, a fourth waiter races it so a losing
        task bails in milliseconds (returning False) instead of sitting the full
        30s once another slot has won — avoiding wasted latency and spurious
        diagnostic dumps.

        Race three Playwright waiters concurrently and return True on the
        first success, cutting 0–1900 ms off the old 2-s polling tick:

          1. wait_for_selector(checkout_container)
          2. wait_for_url(predicate)        — URL has /checkout/, /reservation/, /book/
          3. wait_for_function(payment_visible_js) — payment form visible
                                                     AND URL precondition

        Returns False after a 30 s overall timeout (all three waiters
        either raised or did not resolve). Losing waiters are cancelled and
        awaited so Python doesn't warn about un-awaited coroutines.
        """
        key = "checkout_container"
        selector = sel.get(key)
        total_wait = timeout_sec
        timeout_ms = total_wait * 1000

        async def _via_selector():
            await page.wait_for_selector(selector, timeout=timeout_ms)
            return "selector"

        async def _via_url():
            await page.wait_for_url(_checkout_url_matches, timeout=timeout_ms)
            return "url"

        async def _via_function():
            await page.wait_for_function(_PAYMENT_VISIBLE_JS_SOURCE, timeout=timeout_ms)
            return "function"

        tasks = [
            asyncio.create_task(_via_selector(), name="wait_for_checkout::selector"),
            asyncio.create_task(_via_url(), name="wait_for_checkout::url"),
            asyncio.create_task(_via_function(), name="wait_for_checkout::function"),
        ]
        if booking_won is not None:
            async def _via_aborted():
                await booking_won.wait()
                return "__aborted__"
            tasks.append(
                asyncio.create_task(_via_aborted(), name="wait_for_checkout::aborted")
            )
        won_via: str | None = None
        try:
            for fut in asyncio.as_completed(tasks, timeout=total_wait):
                try:
                    label = await fut
                except Exception as e:
                    logger.debug(
                        f"[book] checkout waiter raised: {type(e).__name__}: {e}"
                    )
                    continue
                won_via = label
                break
        except asyncio.TimeoutError:
            pass
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
            # Drain cancellations so Python doesn't warn about un-awaited
            # coroutines and so any stray Playwright cleanup runs.
            await asyncio.gather(*tasks, return_exceptions=True)

        if won_via == "__aborted__":
            logger.info(
                f"[book] Checkout wait aborted — another slot won "
                f"({slot.slot_date_str})"
            )
            return False
        if won_via:
            logger.info(
                f"[book] Checkout detected via {won_via} for {slot.slot_date_str}"
            )
            return True

        url = page.url
        await self._booking_screenshot(page, "checkout_timeout_final")
        logger.error(
            f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
            f"  Checkout page not detected after {total_wait}s.\n"
            f"  Current URL: {url}\n"
            f"  → Update src/selectors.py"
        )
        return False

    async def _booking_screenshot(self, page: Page, step: str) -> None:
        """Save a screenshot at *step* during booking (only when debug_screenshots=True)."""
        if not self.config.debug_screenshots:
            return
        try:
            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            path = os.path.join(_SCREENSHOT_DIR, f"booking_{ts}_{step}.png")
            await page.screenshot(path=path, full_page=True)
            logger.info(f"[book] Screenshot saved: {path}")
        except Exception as e:
            logger.debug(f"[book] Screenshot failed at step '{step}': {e}")

    async def _prepare_for_confirm(
        self, page: Page, slot: AvailableSlot,
        booking_won: asyncio.Event | None = None,
    ) -> bool:
        """Per-page idempotent preparation for the confirm click. Runs
        WITHOUT the lock so N racing tasks can prep concurrently
        (Phase B3.1).

        Steps:
          - payment detection
          - wait up to 9 min for the operator to add a card if missing
          - CVC fill on the saved card (if any)
          - wait for the confirm button to be visible

        Returns True iff the page is in a ready-to-click state. Returns
        False early if `booking_won` is set during the payment-card
        wait (Codex B3 review MEDIUM 1: avoids N*4 reloads/min when
        N racing tasks are all waiting for the same card to appear).
        """
        needs_payment = await self._page_needs_payment(page)
        has_card = await self._has_saved_card(page)

        if needs_payment and not has_card:
            self.notifier.no_payment_method(slot)
            logger.warning(
                f"[book] Waiting up to {PAYMENT_WAIT_TIMEOUT_SEC}s for a payment "
                f"card to be added to the Tock account…"
            )
            waited = 0
            while waited < PAYMENT_WAIT_TIMEOUT_SEC:
                # Codex B3 MEDIUM 1: short-circuit if another racing task
                # already won — no point reloading our page every 15s.
                if booking_won is not None and booking_won.is_set():
                    logger.info(
                        "[book] Another slot already booked while waiting "
                        "for payment card. Aborting this task's wait."
                    )
                    return False
                await asyncio.sleep(PAYMENT_POLL_INTERVAL_SEC)
                waited += PAYMENT_POLL_INTERVAL_SEC
                if booking_won is not None and booking_won.is_set():
                    logger.info(
                        "[book] booking_won fired during sleep; aborting wait."
                    )
                    return False
                await page.reload(wait_until="domcontentloaded")
                await page.wait_for_timeout(2000)
                if await self._has_saved_card(page):
                    logger.info("[book] Payment card detected. Proceeding to confirm.")
                    break
                logger.info(
                    f"[book] Still waiting for payment card… "
                    f"({waited}/{PAYMENT_WAIT_TIMEOUT_SEC}s)"
                )
            else:
                logger.error(
                    "[book] Timed out waiting for payment card. Aborting this slot."
                )
                return False

        # Fill CVC for saved card if configured (per-page; idempotent)
        if self.config.card_cvc:
            await self._fill_cvc(page)
        elif needs_payment and has_card:
            logger.warning(
                "[book] Saved card detected but TOCK_CARD_CVC is not set in .env — "
                "checkout may fail if CVC is required."
            )

        # Wait once for the confirm button to be visible. Done in prep so
        # the lock-protected click section is as short as possible.
        confirm_selector = sel.get("confirm_button")
        try:
            await page.wait_for_selector(confirm_selector, timeout=15000)
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='confirm_button'  selector={confirm_selector!r}\n"
                f"  Confirm button not found on page.\n"
                f"  Current URL: {page.url}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False
        return True

    async def _execute_confirm_click_and_verify(
        self, page: Page, slot: AvailableSlot
    ) -> bool:
        """Click the confirm button and verify Tock accepted the booking.
        MUST be called under `self._confirm_lock` (Phase B3.1).

        Returns True only if Tock's confirmation page rendered. Returns
        False if the click failed OR the confirmation could not be
        verified — the caller is responsible for soft-win bookkeeping.
        """
        confirm_selector = sel.get("confirm_button")
        for click_attempt in range(2):
            try:
                await page.click(confirm_selector)
                logger.info("[book] Clicked confirm button.")
                break
            except Exception as e:
                if click_attempt == 0:
                    logger.warning(
                        f"[book] Confirm click failed, retrying in 2s: {e}"
                    )
                    await asyncio.sleep(2)
                else:
                    logger.error(
                        f"SELECTOR_FAILED: key='confirm_button'  "
                        f"selector={confirm_selector!r}\n"
                        f"  Could not click the confirm button after 2 attempts.\n"
                        f"  Current URL: {page.url}\n"
                        f"  → Update src/selectors.py  Error: {e}"
                    )
                    return False

        # Verify confirmation.
        # Use 30s timeout (vs 20s) to handle slow Tock servers under heavy traffic.
        # If element not found, fall back to URL check twice (immediate + after 5s delay).
        confirmed_key = "booking_confirmed"
        confirmed_selector = sel.get(confirmed_key)
        try:
            await page.wait_for_selector(confirmed_selector, timeout=30000)
            logger.info(f"[book] Confirmation element found — BOOKED: {slot}")
            return True
        except Exception:
            url = page.url
            if any(p in url for p in ("confirmation", "confirmed", "success")):
                logger.info(f"[book] Booking confirmed via URL: {url}")
                return True
            # Server may be very slow to redirect under high traffic — wait 5s more
            logger.warning(
                "[book] Confirmation page not detected yet — waiting 5s for slow server…"
            )
            await asyncio.sleep(5)
            url = page.url
            if any(p in url for p in ("confirmation", "confirmed", "success")):
                logger.info(f"[book] Booking confirmed via URL (delayed): {url}")
                return True
            logger.error(
                f"SELECTOR_FAILED: key='{confirmed_key}'  selector={confirmed_selector!r}\n"
                f"  Confirmation page not detected after clicking confirm.\n"
                f"  Current URL: {url}\n"
                f"  → Check if booking actually succeeded, then update src/selectors.py"
            )
            return False

    # Codex holistic review: the `_confirm_booking` shim was removed
    # because (a) no production code in src/ called it, (b) it was
    # semantically WEAKER than `_book_single` (no soft-win persistence,
    # no notify, no shared booking_won.set), so any future caller would
    # have silently lost the safety bookkeeping. Tests now patch the
    # split helpers `_prepare_for_confirm` and
    # `_execute_confirm_click_and_verify` directly.

    # ------------------------------------------------------------------
    # Payment detection helpers
    # ------------------------------------------------------------------

    async def _page_needs_payment(self, page: Page) -> bool:
        """True if the checkout page shows any payment-related UI."""
        try:
            el = await page.query_selector(sel.get("no_payment_indicator"))
            if el:
                return True
            el2 = await page.query_selector(sel.get("saved_payment_card"))
            if el2 is not None:
                return True
            # Fallback: CVC input visible ⇒ saved card present ⇒ payment needed
            cvc_el = await self.browser.find_in_frames(page, sel.get("cvc_input"))
            return cvc_el is not None
        except Exception:
            return False

    async def _fill_cvc(self, page: Page) -> None:
        """Fill the CVC field on the checkout page if it exists.

        Tock/Stripe may embed the CVC input inside an iframe, so we search
        both the main frame and all child frames.
        """
        selector = sel.get("cvc_input")
        # TockBrowser.find_in_frames searches main frame + all iframes (Stripe embeds CVC)
        el = await self.browser.find_in_frames(page, selector)
        if el:
            await el.fill(self.config.card_cvc)
            logger.info("[book] CVC filled.")
        else:
            logger.debug("[book] CVC field not found on page (may not be required).")

    async def _has_saved_card(self, page: Page) -> bool:
        """True if a saved payment card widget is visible.

        Tock only renders the CVC re-entry field when a card is already on
        file, so CVC presence is used as a reliable fallback when the
        saved-card widget selector doesn't match.
        """
        try:
            el = await page.query_selector(sel.get("saved_payment_card"))
            if el is not None:
                return True
            # Fallback: CVC input visible ⇒ card is on file
            cvc_el = await self.browser.find_in_frames(page, sel.get("cvc_input"))
            return cvc_el is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Generic selector wait helper
    # ------------------------------------------------------------------

    async def _wait_for_selector(
        self, page: Page, key: str, context: str = "", timeout: int = 10000
    ) -> bool:
        selector = sel.get(key)
        try:
            await page.wait_for_selector(selector, timeout=timeout)
            return True
        except Exception as e:
            logger.error(
                f"SELECTOR_FAILED: key='{key}'  selector={selector!r}\n"
                f"  Context: {context}\n"
                f"  → Update src/selectors.py  Error: {e}"
            )
            return False

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def _best_per_date(self, slots: list[AvailableSlot]) -> list[AvailableSlot]:
        """
        From a list of slots (potentially many per date), return the single
        best slot per calendar date. Assumes slots are already sorted by
        proximity to preferred_time (checker does this), so first per date wins.
        """
        seen: set[str] = set()
        best: list[AvailableSlot] = []
        for slot in slots:
            if slot.slot_date_str not in seen:
                seen.add(slot.slot_date_str)
                best.append(slot)
        return best
