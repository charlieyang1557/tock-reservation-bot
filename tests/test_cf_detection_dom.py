"""Tests for DOM-based Cloudflare challenge detection (Phase B2.2).

Old behavior: URL-substring match only. CF challenges that landed
without changing the URL (e.g., pure interstitial overlays) slipped
through and the bot wasted polling time on a page that was actually
blocked.

New behavior: combine URL match (fast, sync) with a DOM check that
runs `page.evaluate` looking for any of:
  - iframe[src*="challenges.cloudflare.com"]
  - .cf-turnstile widget
  - #cf-please-wait / #cf-spinner-please-wait
  - "verify you are human" / "just a moment" / "checking your browser"
    text in any visible h1/h2/p/div
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailabilityChecker
from src.config import Config


def _make_checker():
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


def _make_page(url: str, dom_signal: bool = False):
    page = AsyncMock()
    page.url = url
    page.evaluate = AsyncMock(return_value=bool(dom_signal))
    return page


# ---------------------------------------------------------------------------
# URL-only existing path still works (backwards compat)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detects_via_url_challenge_substring():
    """Existing URL match still fires (no DOM check needed)."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/challenge?ray=abc",
        dom_signal=False,  # DOM says clean — URL alone is enough
    )
    assert await checker.is_cloudflare_challenge_page(page) is True


@pytest.mark.asyncio
async def test_detects_via_cf_chl_query_param():
    """`__cf_chl` query param signals a CF challenge."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/path?__cf_chl_tk=xyz",
        dom_signal=False,
    )
    assert await checker.is_cloudflare_challenge_page(page) is True


# ---------------------------------------------------------------------------
# NEW: DOM check fires when URL is clean but DOM has CF markers
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_detects_via_dom_when_url_is_clean():
    """A page whose URL is normal but DOM contains a CF iframe / turnstile
    widget / 'verify you are human' text must be detected."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/test/search",
        dom_signal=True,  # JS reports DOM markers present
    )
    assert await checker.is_cloudflare_challenge_page(page) is True


@pytest.mark.asyncio
async def test_returns_false_when_neither_url_nor_dom_signals_present():
    """Normal page with no CF markers anywhere → False."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/test/search",
        dom_signal=False,
    )
    assert await checker.is_cloudflare_challenge_page(page) is False


@pytest.mark.asyncio
async def test_dom_check_failure_does_not_brick_detection():
    """If page.evaluate raises (page closed, context destroyed, etc.),
    fall back to the URL signal — never raise out of detection."""
    checker = _make_checker()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.evaluate = AsyncMock(side_effect=Exception("page closed"))

    # Should not raise; URL is clean, DOM check failed → False
    assert await checker.is_cloudflare_challenge_page(page) is False


@pytest.mark.asyncio
async def test_dom_check_failure_with_cf_url_still_returns_true():
    """If URL match fires AND DOM check raises, still return True
    (URL is sufficient on its own)."""
    checker = _make_checker()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/challenge?ray=abc"
    page.evaluate = AsyncMock(side_effect=Exception("page closed"))

    assert await checker.is_cloudflare_challenge_page(page) is True


# ---------------------------------------------------------------------------
# Static URL-only helper preserved for fast non-blocking checks
# ---------------------------------------------------------------------------

def test_url_only_helper_still_available():
    """The synchronous URL-only helper is preserved (some callers don't
    want to await an evaluate)."""
    page_sync = MagicMock()
    page_sync.url = "https://www.exploretock.com/challenge"
    assert AvailabilityChecker._is_cloudflare_challenge_page(page_sync) is True

    page_sync.url = "https://www.exploretock.com/test/search"
    assert AvailabilityChecker._is_cloudflare_challenge_page(page_sync) is False


# ---------------------------------------------------------------------------
# JS source contract
# ---------------------------------------------------------------------------

def test_dom_js_source_includes_cloudflare_markers():
    """The JS source string must reference the documented CF DOM markers
    so a future refactor can't accidentally drop them."""
    from src.checker import _CF_DOM_DETECT_JS

    src = _CF_DOM_DETECT_JS
    assert "challenges.cloudflare.com" in src
    assert "cf-turnstile" in src or "cf-please-wait" in src
    # Text-based signals
    assert (
        "verify you are human" in src.lower()
        or "just a moment" in src.lower()
        or "checking your browser" in src.lower()
    )
