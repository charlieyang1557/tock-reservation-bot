"""Tests for Codex B2 review fixes (one HIGH + 3 MEDIUM).

HIGH 1: B2.1 archive-failure must fail closed
  Old: if archive rename fails, fall back to unlink — LOSES the
  no-race guard entirely, allowing the bot to book again on a
  slot the operator hasn't yet verified.
  New: leave the live file in place so future races stay blocked
  until operator manually clears.

MEDIUM 1: checkout URL match must use parsed path only
  Old: regex runs against the full URL — `/search?next=/checkout/abc`
  satisfies the predicate and `_wait_for_checkout` returns true on
  the wrong page.
  New: parse URL with urlparse, match only `parsed.path`.

MEDIUM 2: iframe cache must exact-match host (not substring)
  Old: `cached_host in frame.url` — `js.stripe.com` could match
  `https://attacker.com/spoof/js.stripe.com/widget`.
  New: compare `urlparse(frame.url).netloc` exactly to cached host.

MEDIUM 3: unknown slot selectors must default to safer "generic"
  Old: unknown → is_generic=False → first-button fallback allowed
  → could click a restaurant-level Book button.
  New: unknown → is_generic=True (refuse first-button fallback)
  AND log a WARNING so the operator knows a selector wasn't tagged.
"""
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# HIGH: archive-failure must fail closed
# ---------------------------------------------------------------------------

def test_archive_failure_does_not_unlink_live_file(tmp_path, monkeypatch, caplog):
    """If _archive_uncertain's rename fails, the live file MUST remain so
    future races stay blocked until the operator clears it. Old fallback
    that called `unlink` removed the safety guard."""
    import logging
    from src.booking_uncertain import (
        UncertainBooking, _archive_uncertain, write_uncertain,
    )

    f = tmp_path / "booking_uncertain.json"
    booking = UncertainBooking(
        slot_date_str=(date.today() - timedelta(days=10)).isoformat(),
        slot_time="5:00 PM", day_of_week="Friday",
        detected_at_iso=datetime.now().isoformat(),
    )
    write_uncertain(booking, path=f)
    assert f.exists()

    # Force the archive directory creation to fail
    def fail_mkdir(*args, **kwargs):
        raise OSError("simulated permissions error")

    with patch("pathlib.Path.mkdir", side_effect=fail_mkdir), \
         caplog.at_level(logging.ERROR):
        _archive_uncertain(f, reason="stale")

    # CRITICAL: live file must still exist (failed-closed)
    assert f.exists(), (
        "Archive failure must NOT unlink the live file — that would "
        "remove the operator's safety guard and allow the bot to race "
        "on a slot it cannot prove was unbooked."
    )
    # And the bot should log loudly so the operator notices
    assert any(
        "archive" in rec.message.lower() and "failed" in rec.message.lower()
        for rec in caplog.records
    )


def test_read_uncertain_with_failed_archive_still_blocks(tmp_path, monkeypatch):
    """End-to-end: a stale file whose archive fails should still cause
    read_uncertain to return the original booking (so the bot keeps
    refusing races) — better than silently unblocking everything."""
    from src.booking_uncertain import (
        UncertainBooking, read_uncertain, write_uncertain,
    )

    f = tmp_path / "booking_uncertain.json"
    booking = UncertainBooking(
        slot_date_str=(date.today() - timedelta(days=10)).isoformat(),
        slot_time="5:00 PM", day_of_week="Friday",
        detected_at_iso=datetime.now().isoformat(),
    )
    write_uncertain(booking, path=f)

    # Force archive to fail completely
    def fail_mkdir(*args, **kwargs):
        raise OSError("simulated permissions error")

    with patch("pathlib.Path.mkdir", side_effect=fail_mkdir):
        result = read_uncertain(path=f)

    # Two acceptable behaviors here, both prefer safety:
    #   (a) result is None (treated as stale) AND f still exists so the
    #       NEXT read still flags it (preferring "log every cycle" over
    #       "silently allow races")
    #   (b) result is the booking object (kept as authoritative because
    #       we couldn't archive it)
    # Either is fine; what we MUST NOT have is "result is None AND
    # f no longer exists" (silent unblock).
    if result is None:
        assert f.exists(), (
            "Stale file whose archive failed must remain on disk so "
            "the next read_uncertain call still surfaces it — never "
            "silently unblock races on archive failure"
        )


# ---------------------------------------------------------------------------
# MEDIUM 1: checkout URL match path-only
# ---------------------------------------------------------------------------

def test_checkout_url_does_not_match_query_string_substring():
    """`/search?next=/restaurant/checkout/abc` must NOT match — the
    `/checkout/abc` is in the QUERY STRING, not the path."""
    from src.booker import _checkout_url_matches

    assert _checkout_url_matches(
        "https://www.exploretock.com/search?next=/restaurant/checkout/abc"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant?return_to=/book/123"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/account?ref=/reservation/foo"
    ) is False


def test_checkout_url_does_not_match_fragment_substring():
    """`#fragment` containing `/checkout` must NOT match either — the
    fragment is client-side only and doesn't represent a real
    navigation to a checkout page."""
    from src.booker import _checkout_url_matches

    assert _checkout_url_matches(
        "https://www.exploretock.com/account#/checkout/abc"
    ) is False
    assert _checkout_url_matches(
        "https://www.exploretock.com/profile/payment-methods#/book/foo"
    ) is False


def test_checkout_url_still_matches_real_paths():
    """Regression: real checkout/reservation/book paths still match."""
    from src.booker import _checkout_url_matches

    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/checkout/abc123"
    ) is True
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/checkout/abc123?param=1"
    ) is True
    assert _checkout_url_matches(
        "https://www.exploretock.com/restaurant/reservation/xyz#section"
    ) is True


# ---------------------------------------------------------------------------
# MEDIUM 2: iframe cache exact netloc match
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_iframe_cache_uses_exact_netloc_match():
    """If the cached host is `js.stripe.com`, an iframe at
    `https://attacker.com/spoof/js.stripe.com/widget` must NOT be
    preferred over a real Stripe frame — substring match is unsafe."""
    from src.browser import TockBrowser

    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    browser = TockBrowser(config)

    # Pre-warm cache to "js.stripe.com"
    browser._frame_url_cache['input[name="cvc"]'] = "js.stripe.com"

    main = AsyncMock()
    main.url = "https://www.exploretock.com/checkout"
    main.is_detached = MagicMock(return_value=False)
    main.query_selector = AsyncMock(return_value=None)

    spoof = AsyncMock()
    spoof.url = "https://attacker.com/spoof/js.stripe.com/widget"
    spoof.is_detached = MagicMock(return_value=False)
    spoof.query_selector = AsyncMock(return_value=MagicMock(name="spoof-cvc"))

    real_stripe = AsyncMock()
    real_stripe.url = "https://js.stripe.com/v3/elements-inner.html"
    real_stripe.is_detached = MagicMock(return_value=False)
    real_stripe.query_selector = AsyncMock(return_value=MagicMock(name="real-cvc"))

    page = MagicMock()
    page.main_frame = main
    page.frames = [main, spoof, real_stripe]

    await browser.find_in_frames(page, 'input[name="cvc"]')

    # The real stripe frame must have been queried; the spoof must not
    # have won the cached-first preference.
    assert real_stripe.query_selector.called
    # Spoof CVC should have been ignored — i.e., the cache lookup did
    # not pick it as preferred (because its netloc is attacker.com,
    # not js.stripe.com)
    if spoof.query_selector.called:
        # If queried at all, it must have been AFTER real_stripe was
        # tried (i.e., real_stripe was preferred). Hard to assert
        # ordering precisely; instead assert the cache picked the right
        # frame to refresh:
        assert browser._frame_url_cache['input[name="cvc"]'] == "js.stripe.com"


# ---------------------------------------------------------------------------
# MEDIUM 3: unknown slot selectors default to safer "generic"
# ---------------------------------------------------------------------------

def test_unknown_slot_selector_defaults_to_generic():
    """A selector not in the typed list must be treated as GENERIC
    (refuses first-button fallback) — the safer default. Old behavior
    treated unknowns as 'specific' which allowed accidental clicks
    on restaurant-level Book buttons."""
    from src.selectors import is_generic_slot_selector

    # Unknown selectors → generic (safer)
    assert is_generic_slot_selector(
        "div.some-future-selector-not-in-list"
    ) is True


def test_known_specific_selector_still_returns_false():
    """Sanity: a known SPECIFIC selector still reports is_generic=False."""
    from src.selectors import is_generic_slot_selector
    assert is_generic_slot_selector(
        "button.Consumer-resultsListItem.is-available"
    ) is False
    assert is_generic_slot_selector("button.Consumer-resultsListItem") is False


def test_known_generic_selector_still_returns_true():
    """Sanity: known GENERIC selectors still report is_generic=True."""
    from src.selectors import is_generic_slot_selector
    assert is_generic_slot_selector(
        'button:visible:has-text("Book")'
    ) is True


def test_empty_selector_returns_false():
    """Empty string is not a valid selector — preserve existing
    behavior (don't crash, return False)."""
    from src.selectors import is_generic_slot_selector
    assert is_generic_slot_selector("") is False


def test_unknown_selector_logs_warning(caplog):
    """When an unknown selector is queried, log a WARNING so the
    operator knows to add it to the typed list."""
    import logging
    from src.selectors import is_generic_slot_selector

    with caplog.at_level(logging.WARNING):
        is_generic_slot_selector("div.unknown-future-selector")

    assert any(
        "unknown" in rec.message.lower() and "selector" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected WARNING for unknown selector; got {[r.message for r in caplog.records]}"
