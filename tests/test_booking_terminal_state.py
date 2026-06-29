"""Unit 1 (06/26 incident P0b) — detect Tock's post-"Book" TERMINAL state
(race-loss toast / sold-out modal) so the booker fast-aborts instead of
re-clicking a dead Book button for ~84s.

Ground truth: the 4 booking_failures/faildom_checkout_20260626_*.html dumps were
the SEARCH page carrying (1) a MuiSnackbar error toast "Unfortunately, someone
else just selected this and it is no longer available." and (2) an
in-business-search-modal MuiDialog "… has sold out all reservations for 2 guests
on Jul 3.". On 06/26 the booker could not tell it had lost, so _click_until_checkout
re-clicked the live Book button every 2s for all 10 tries × 4 tasks (~84s).

The HTML below is a SANITIZED, structure-faithful reproduction of those dumps
(MuiSnackbar > MuiAlert-message; in-business-search-modal > SearchModal-body) with
the exact user-facing strings; no authenticated markup.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailableSlot


# --- Sanitized real-DOM fixtures (verbatim Tock strings) --------------------

RACE_LOSS_HTML = """<!DOCTYPE html><html><head>
<link rel="canonical" href="https://www.exploretock.com/fui-hui-hua-san-francisco/search"></head>
<body>
  <div data-testid="reservation-search-bar"></div>
  <div data-testid="consumer-calendar"></div>
  <div class="Wrapper">
    <div class="MuiSnackbar-root MuiSnackbar-anchorOriginTopCenter css-zsq44o" aria-live="polite">
      <div class="MuiPaper-root MuiAlert-root MuiAlert-standardError" role="alert">
        <div class="MuiAlert-message">
          <div class="MuiTypography-root css-acoaoq">Unfortunately, someone else just selected this and it is no longer available.</div>
        </div>
        <div class="MuiAlert-action"><button aria-label="Close message">x</button></div>
      </div>
    </div>
  </div>
</body></html>"""

SOLD_OUT_HTML = """<!DOCTYPE html><html><head>
<link rel="canonical" href="https://www.exploretock.com/fui-hui-hua-san-francisco/search"></head>
<body>
  <div data-testid="reservation-search-bar"></div>
  <div class="MuiDialog-root" data-testid="in-business-search-modal" role="dialog">
    <div class="MuiBackdrop-root" aria-hidden="true" style="opacity: 1;"></div>
    <div class="MuiDialog-container"><div class="MuiPaper-root">
      <div class="SearchModal-body">
        <span class="MuiTypography-root"><span class="MuiTypography-root">Fu Hui Hua has sold out all reservations for 2 guests on Jul 3. Try another party size or date</span> or <a class="Anchor">Notify</a>.</span>
      </div>
    </div></div>
  </div>
</body></html>"""

# An AVAILABLE search page (real Tock result cards) — must NOT be terminal.
AVAILABLE_HTML = """<!DOCTYPE html><html><body>
  <div data-testid="search-result">
    <button data-testid="booking-card-button">5:00 PM $258 x 2 Book</button>
  </div>
  <div data-testid="search-result">
    <button data-testid="booking-card-button">8:00 PM $258 x 2 Book</button>
  </div>
</body></html>"""


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
    return TockBooker(config, MagicMock(), MagicMock())


def _make_slot():
    return AvailableSlot(
        slot_date=date(2026, 7, 3), slot_time="5:00 PM", day_of_week="Friday",
    )


# --- classify_booking_terminal_state (pure) ---------------------------------

def test_classify_race_loss():
    from src.booker import classify_booking_terminal_state
    assert classify_booking_terminal_state(RACE_LOSS_HTML) == "race_lost"


def test_classify_sold_out():
    from src.booker import classify_booking_terminal_state
    assert classify_booking_terminal_state(SOLD_OUT_HTML) == "sold_out"


def test_classify_available_page_is_not_terminal():
    from src.booker import classify_booking_terminal_state
    assert classify_booking_terminal_state(AVAILABLE_HTML) is None


def test_classify_empty_is_not_terminal():
    from src.booker import classify_booking_terminal_state
    assert classify_booking_terminal_state("") is None
    assert classify_booking_terminal_state(None) is None


def test_classify_nonstring_is_not_terminal():
    """Defensive: a non-string input (e.g. a mock page.content() result, or a
    page object) must return None rather than raise — otherwise the terminal
    probe would break the booking retry loop (regressed 7 slot-click tests)."""
    from src.booker import classify_booking_terminal_state
    assert classify_booking_terminal_state(MagicMock()) is None
    assert classify_booking_terminal_state(12345) is None


# --- _detect_booking_terminal_state (async wrapper) -------------------------

@pytest.mark.asyncio
async def test_detect_reads_page_content():
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value=RACE_LOSS_HTML)
    assert await booker._detect_booking_terminal_state(page) == "race_lost"


@pytest.mark.asyncio
async def test_detect_swallows_content_errors():
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(side_effect=Exception("page mid-navigation"))
    assert await booker._detect_booking_terminal_state(page) is None


# --- wiring: _click_until_checkout fast-aborts on terminal state ------------

@pytest.mark.asyncio
async def test_click_loop_aborts_on_race_loss_instead_of_hammering():
    """The 06/26 regression: when checkout never loads, the loop must STOP as
    soon as Tock shows the race-loss toast — not re-click 10 times."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/search"
    page.evaluate = AsyncMock()

    # Each try: click succeeds, but checkout never loads.
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    # Tock is showing the race-loss toast from the first failed try onward.
    booker._detect_booking_terminal_state = AsyncMock(return_value="race_lost")

    booking_won = asyncio.Event()
    result = await booker._click_until_checkout(
        page, slot, owns_page=True, skip_click=False,
        day_clicked=False, booking_won=booking_won,
    )

    assert result is False
    # The whole point: we did NOT burn all 10 tries. One real click attempt,
    # detect terminal, abort.
    assert booker._attempt_slot_click.await_count == 1, (
        f"expected fast-abort after 1 click, got "
        f"{booker._attempt_slot_click.await_count} clicks (zombie loop)"
    )


@pytest.mark.asyncio
async def test_click_loop_records_terminal_reason_for_notification():
    """The terminal reason is stored so the failure notification can be
    race-loss-specific (P1)."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/search"
    page.evaluate = AsyncMock()
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    booker._detect_booking_terminal_state = AsyncMock(return_value="sold_out")

    booking_won = asyncio.Event()
    await booker._click_until_checkout(
        page, slot, owns_page=True, skip_click=False,
        day_clicked=False, booking_won=booking_won,
    )
    assert booker.last_terminal_reason == "sold_out"
