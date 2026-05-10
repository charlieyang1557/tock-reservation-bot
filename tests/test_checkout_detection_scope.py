"""Tests for tightened checkout URL matching + scoped payment-visible
JS in `_wait_for_checkout` (Codex LOW 1 + LOW 2 fixes).

LOW 1: the URL predicate previously matched any URL containing the
substring `/book` — `/booklist`, `/bookmark`, etc. would false-positive
into the confirm flow on the wrong page.

LOW 2: the payment_visible_js predicate previously scanned the entire
document for "Add card / Add payment" text — a user's
`/account/payment-methods` page contains identical text and could
trigger checkout detection while the bot is on the wrong page.

Both fixes use the same shared idea: require the URL to look like an
actual checkout URL before treating any signal as positive.
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailableSlot
from src.booker import _checkout_url_matches, _PAYMENT_VISIBLE_JS_SOURCE


# ---------------------------------------------------------------------------
# LOW 1: tighter URL predicate
# ---------------------------------------------------------------------------

def test_checkout_url_matches_recognizes_real_checkout_path():
    """Real Tock URLs end with `/checkout/<id>`, `/reservation/<id>`,
    or `/book/<id>` — the predicate must accept these."""
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/checkout/abc123"
    ) is True
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/reservation/xyz"
    ) is True
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/book/9999"
    ) is True


def test_checkout_url_matches_accepts_path_with_trailing_slash_only():
    """Paths that end exactly with the segment + trailing slash also count
    (some Tock landing flows redirect to `/restaurant/checkout/`)."""
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/checkout/"
    ) is True


def test_checkout_url_rejects_substring_false_positives():
    """`/booklist`, `/bookmark`, etc. must NOT match — the LOW 1 bug was
    accepting these and racing to confirm on the wrong page."""
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/booklist"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/bookmark"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/checkoutlite"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/search?date=2026-05-15"
    ) is False


def test_checkout_url_rejects_account_pages():
    """The user's profile/payment-methods pages must NOT match — these
    are the LOW 2 false-positive surface."""
    assert _checkout_url_matches(
        "https://www.exploretock.com/account/payment-methods"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/account/reservations"
    ) is False


# ---------------------------------------------------------------------------
# LOW 2: payment_visible_js requires URL precondition
# ---------------------------------------------------------------------------

def test_payment_visible_js_includes_url_precondition():
    """Inspect the JS source string: it must check window.location for
    a checkout-style path before scanning text. Belt-and-suspenders
    against the `/account/payment-methods` false-positive."""
    src = _PAYMENT_VISIBLE_JS_SOURCE
    # The JS must consult window.location.pathname (or .href) so the URL
    # check is performed inside the page context, not just in Python.
    assert "window.location" in src or "location.pathname" in src or "location.href" in src, (
        f"payment_visible_js must include a URL precondition; got:\n{src}"
    )
    # The JS must reference at least one of the checkout path segments
    assert any(seg in src for seg in ("checkout", "reservation", "/book")), (
        "payment_visible_js must check for /checkout, /reservation, or /book "
        "in the URL"
    )


# ---------------------------------------------------------------------------
# End-to-end: _wait_for_checkout still uses the tightened predicate
# ---------------------------------------------------------------------------

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
        slot_date=date(2026, 5, 15), slot_time="5:00 PM",
        day_of_week="Friday",
    )


@pytest.mark.asyncio
async def test_wait_for_checkout_passes_tightened_predicate():
    """`_wait_for_checkout` must call `wait_for_url(predicate)` where the
    predicate is the tightened `_checkout_url_matches` function."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    # Capture the predicate
    captured = {}
    async def fake_wait_for_url(predicate, timeout=None):
        captured["predicate"] = predicate
        await __import__("asyncio").sleep(0.01)
        return None
    page.wait_for_url = AsyncMock(side_effect=fake_wait_for_url)
    page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    page.wait_for_function = AsyncMock(side_effect=Exception("timeout"))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    await booker._wait_for_checkout(page, slot)

    pred = captured.get("predicate")
    assert pred is not None
    # The predicate must reject false positives
    assert pred("https://x/booklist") is False
    assert pred("https://x/restaurant/checkout/abc") is True
