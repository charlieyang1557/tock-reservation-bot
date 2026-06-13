"""Fix 1 (P0) — the booker must click the correct slot's Book button in
Tock's MUI search-result layout (the 2026-06-12 regression).

Tock migrated the slot button to button[data-testid="booking-card-button"]
inside a div[data-testid="search-result"] card. The time label sits in a
SIBLING subtree of the button, so the old matcher (button text / immediate
parent only) found no time match and refused to click an enabled Book
button. The fix:
  • adds the booking-card-button selector to the cascade (before the
    fragile generic :has-text("Book")), and
  • scopes the time confirmation to the enclosing search-result card in
    BOTH matcher paths (the JS fast path and the Playwright locator loop).

The real-browser tests drive the actual matcher against a sanitized
fixture (tests/fixtures/tock_search_result_cards.html) — no production DOM.
"""
import os
from datetime import date
from unittest.mock import MagicMock

import pytest

from src.checker import AvailableSlot

_FIXTURE = os.path.join(
    os.path.dirname(__file__), "fixtures", "tock_search_result_cards.html"
)
_BOOKING_CARD_SEL = 'button[data-testid="booking-card-button"]'
_GENERIC_BOOK_SEL = 'button:visible:has-text("Book")'

# Records which card's time text received the click, so a test can assert
# the matcher clicked the RIGHT slot (not just any Book button).
_RECORDER_JS = """
() => {
  window.__clickedTime = null;
  document.addEventListener('click', (e) => {
    const btn = e.target.closest('[data-testid="booking-card-button"]');
    if (!btn) return;
    const card = btn.closest('[data-testid="search-result"]');
    const t = card && card.querySelector('[data-testid="search-result-time"]');
    window.__clickedTime = t ? t.textContent.trim() : '(no-time)';
  }, true);
}
"""


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="fui-hui-hua",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return TockBooker(config, MagicMock(), MagicMock())


def _slot(slot_time: str):
    return AvailableSlot(
        slot_date=date(2026, 6, 19), slot_time=slot_time, day_of_week="Friday",
    )


# ---------------------------------------------------------------------------
# Pure selector wiring (no browser)
# ---------------------------------------------------------------------------

def test_booking_card_button_is_in_the_cascade():
    from src.selectors import get_slot_button_selectors
    assert _BOOKING_CARD_SEL in get_slot_button_selectors()


def test_booking_card_button_is_generic_and_plain_css():
    """Generic → time confirmed via surrounding card (button text is just
    'Book'). Plain CSS → safe for the JS fast path (querySelectorAll)."""
    from src.selectors import is_generic_slot_selector, is_playwright_selector
    assert is_generic_slot_selector(_BOOKING_CARD_SEL) is True
    assert is_playwright_selector(_BOOKING_CARD_SEL) is False


def test_booking_card_button_ranked_before_fragile_generic_book():
    """The precise data-testid selector must be tried before the loose
    :has-text('Book') fallback that mis-fired on 06/12."""
    from src.selectors import get_slot_button_selectors
    sels = get_slot_button_selectors()
    assert sels.index(_BOOKING_CARD_SEL) < sels.index(_GENERIC_BOOK_SEL)


# ---------------------------------------------------------------------------
# Real-browser matcher behaviour against the sanitized fixture
# ---------------------------------------------------------------------------

def _two_card_html(time_a: str, time_b: str) -> str:
    """Two search-result cards (time_a first in DOM order, then time_b), each
    with a card-level booking-card-button. Used to prove the matcher picks the
    card whose time TOKEN-matches the target — not a substring sibling."""
    def card(t):
        return (
            '<div class="MuiCard-root" data-testid="search-result">'
            '<div class="MuiCardHeader-content"><span class="x">'
            '<section data-testid="result-time-section">'
            f'<span data-testid="search-result-time"><span>{t}</span></span>'
            '<span><span data-testid="result-price"><span>$258</span> &times; 2</span></span>'
            '</section></span></div>'
            '<div class="MuiCardActions-root">'
            '<button data-testid="booking-card-button"><span>Book</span></button>'
            '</div></div>'
        )
    return f"<!doctype html><html><body>{card(time_a)}{card(time_b)}</body></html>"


async def _drive(slot, force_selector=None, html=None):
    """Load the fixture (or inline `html`) in a real chromium page and run the
    matcher. Returns (result_bool, clicked_time_text). Skips if chromium absent."""
    from playwright.async_api import async_playwright
    if html is None:
        with open(_FIXTURE, encoding="utf-8") as f:
            html = f.read()
    booker = _make_booker()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover - env without chromium
            pytest.skip(f"chromium unavailable: {e}")
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="domcontentloaded")
            await page.evaluate(_RECORDER_JS)
            if force_selector is None:
                result = await booker._click_time_slot(page, slot)
            else:
                result = await booker._click_time_slot_locator_loop(
                    page, slot, force_selector, is_generic=True,
                    strict_time_match=False,
                )
            clicked = await page.evaluate("() => window.__clickedTime")
        finally:
            await browser.close()
    return result, clicked


@pytest.mark.asyncio
async def test_js_path_clicks_8pm_card():
    """The 06/12 repro: target 8:00 PM → the 8:00 PM card's Book button is
    clicked (was: 'No clickable slot button found')."""
    result, clicked = await _drive(_slot("8:00 PM"))
    assert result is True
    assert clicked == "8:00 PM"


@pytest.mark.asyncio
async def test_js_path_clicks_5pm_card_not_8pm():
    """Discrimination: target 5:00 PM clicks the 5:00 PM card, proving the
    matcher isn't just clicking the first Book button."""
    result, clicked = await _drive(_slot("5:00 PM"))
    assert result is True
    assert clicked == "5:00 PM"


@pytest.mark.asyncio
async def test_locator_loop_card_scopes_generic_book_button():
    """Defense in depth: even via the legacy generic :has-text('Book')
    selector (the one that actually ran on 06/12), the locator loop must
    confirm the time from the enclosing card and click the 8:00 PM button."""
    result, clicked = await _drive(_slot("8:00 PM"), force_selector=_GENERIC_BOOK_SEL)
    assert result is True
    assert clicked == "8:00 PM"


# ---------------------------------------------------------------------------
# Substring-collision regression (code-review CONFIRMED): "1:00 PM" must NOT
# match the "11:00 PM" card. Card-scoping widened the matched text, so a naive
# `target in cardText` clicks the wrong slot. Require TOKEN (word-boundary) match.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_js_path_does_not_match_substring_sibling_time():
    """Target 1:00 PM with an 11:00 PM card FIRST in DOM order. '1:00 PM' is a
    substring of '11:00 PM' — the matcher must still click the 1:00 PM card."""
    html = _two_card_html("11:00 PM", "1:00 PM")
    result, clicked = await _drive(_slot("1:00 PM"), html=html)
    assert result is True
    assert clicked == "1:00 PM", f"clicked the wrong (substring) card: {clicked}"


@pytest.mark.asyncio
async def test_locator_loop_does_not_match_substring_sibling_time():
    html = _two_card_html("11:00 PM", "1:00 PM")
    result, clicked = await _drive(
        _slot("1:00 PM"), force_selector=_GENERIC_BOOK_SEL, html=html
    )
    assert result is True
    assert clicked == "1:00 PM", f"clicked the wrong (substring) card: {clicked}"


# The EXACT branch (button's OWN text is the time — legacy Consumer-resultsListItem
# layout) had the same substring bug: '1:00 PM' matched an '11:00 PM' button's text.
# (codex round-2 CONFIRMED — the card-branch fix didn't cover this.)

_TIME_BUTTON_HTML = (
    "<!doctype html><html><body><div>"
    '<button class="Consumer-resultsListItem is-available"><span>11:00 PM</span></button>'
    '<button class="Consumer-resultsListItem is-available"><span>1:00 PM</span></button>'
    "</div></body></html>"
)
_BTN_RECORDER_JS = """
() => {
  window.__clickedTime = null;
  document.addEventListener('click', (e) => {
    const b = e.target.closest('button');
    if (b) window.__clickedTime = b.textContent.trim();
  }, true);
}
"""


async def _drive_time_buttons(slot, force_selector=None):
    from playwright.async_api import async_playwright
    booker = _make_booker()
    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
        except Exception as e:  # pragma: no cover
            pytest.skip(f"chromium unavailable: {e}")
        try:
            page = await browser.new_page()
            await page.set_content(_TIME_BUTTON_HTML, wait_until="domcontentloaded")
            await page.evaluate(_BTN_RECORDER_JS)
            if force_selector is None:
                result = await booker._click_time_slot(page, slot)
            else:
                result = await booker._click_time_slot_locator_loop(
                    page, slot, force_selector, is_generic=False,
                    strict_time_match=False,
                )
            clicked = await page.evaluate("() => window.__clickedTime")
        finally:
            await browser.close()
    return result, clicked


@pytest.mark.asyncio
async def test_js_exact_branch_no_substring_match_on_button_text():
    """target 1:00 PM, buttons '11:00 PM' then '1:00 PM' — the exact-branch
    button-text check must not substring-match 11:00 PM."""
    result, clicked = await _drive_time_buttons(_slot("1:00 PM"))
    assert result is True
    assert clicked == "1:00 PM", f"exact branch clicked the substring button: {clicked}"


@pytest.mark.asyncio
async def test_locator_loop_exact_no_substring_match_on_button_text():
    result, clicked = await _drive_time_buttons(
        _slot("1:00 PM"), force_selector='button:visible:has-text("PM")'
    )
    assert result is True
    assert clicked == "1:00 PM", f"locator-loop exact clicked the substring button: {clicked}"
