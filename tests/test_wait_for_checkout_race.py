"""Tests for the race-of-waiters _wait_for_checkout (Phase B1.1).

Old behavior: poll every 2s for up to 30s, checking three signals serially
each tick (wait_for_selector with 2s timeout, then URL substring, then
query_selector for payment element).

New behavior: schedule three Playwright waiters concurrently
(wait_for_selector, wait_for_url predicate, wait_for_function for payment
visible) and return as soon as ANY ONE resolves. Cancel + await the
losing waiters so no warnings about un-awaited coroutines slip through.

Contracts that MUST be preserved:
  - returns True on success (any of the three signals resolves)
  - returns False on full timeout (all three time out)
  - debug screenshot is still taken on timeout
  - no DeprecationWarning / RuntimeWarning about un-awaited cancellations
"""
import asyncio
import warnings
from pathlib import Path
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot


def _make_booker(debug_screenshots: bool = False):
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=debug_screenshots,
        discord_webhook_url="", card_cvc="",
    )
    browser = MagicMock()
    notifier = MagicMock()
    return TockBooker(config, browser, notifier)


def _make_slot():
    return AvailableSlot(
        slot_date=date(2026, 5, 15),
        slot_time="5:00 PM",
        day_of_week="Friday",
    )


def _waiter(result, *, delay: float = 0.0, raise_exc: Exception | None = None):
    """Build a coroutine factory that returns `result` after `delay`,
    or raises `raise_exc` instead. Used as `side_effect` on AsyncMocks."""

    async def _impl(*args, **kwargs):
        if delay:
            await asyncio.sleep(delay)
        if raise_exc is not None:
            raise raise_exc
        return result

    return _impl


def _timeout_exc():
    """A Playwright-shaped TimeoutError. Use a regular Exception subclass so
    asyncio.gather + return_exceptions doesn't propagate it as cancellation."""
    return Exception("timeout — test")


@pytest.mark.asyncio
async def test_returns_on_url_change_first():
    """If wait_for_url resolves first, the function returns True."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/abc"
    # selector waiter never resolves until we cancel it
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, delay=10))
    # url waiter wins
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, delay=0.01))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, delay=10))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is True
    # Losers should have been cancelled (called, then cancelled — we don't
    # need to assert cancel directly; the absence of warnings is enough)
    assert page.wait_for_url.called


@pytest.mark.asyncio
async def test_returns_on_selector_first():
    """If wait_for_selector resolves first, the function returns True."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=_waiter(MagicMock(), delay=0.01))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, delay=10))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, delay=10))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is True
    assert page.wait_for_selector.called


@pytest.mark.asyncio
async def test_returns_on_payment_element_first():
    """If wait_for_function (payment visible) resolves first, returns True."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, delay=10))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, delay=10))
    page.wait_for_function = AsyncMock(side_effect=_waiter(MagicMock(), delay=0.01))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is True
    assert page.wait_for_function.called


@pytest.mark.asyncio
async def test_returns_false_when_all_time_out():
    """If all three waiters time out, the function returns False."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is False


@pytest.mark.asyncio
async def test_screenshot_taken_on_full_timeout(tmp_path):
    """When all waiters time out and debug_screenshots=True, a screenshot
    is taken (preserves the existing checkout_timeout_final screenshot)."""
    booker = _make_booker(debug_screenshots=True)
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.query_selector = AsyncMock(return_value=None)

    saved = []
    async def fake_screenshot(path=None, **_kw):
        if path:
            saved.append(path)
            Path(path).parent.mkdir(parents=True, exist_ok=True)
            Path(path).write_bytes(b"PNG")
    page.screenshot = AsyncMock(side_effect=fake_screenshot)

    with patch("src.booker._SCREENSHOT_DIR", str(tmp_path)):
        result = await booker._wait_for_checkout(page, slot)
    assert result is False
    assert any("checkout_timeout_final" in s for s in saved), (
        f"Expected a checkout_timeout_final screenshot; got: {saved}"
    )


@pytest.mark.asyncio
async def test_cancels_losing_waiters_without_warnings():
    """When the winner returns, the other two waiters are cancelled and
    awaited so Python does not warn about un-awaited cancellations."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/abc"
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, delay=5))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, delay=0.01))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, delay=5))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = await booker._wait_for_checkout(page, slot)
        # Yield the loop a few times so any pending cancellations would surface
        for _ in range(3):
            await asyncio.sleep(0)
    assert result is True
    bad = [
        str(w.message) for w in caught
        if "coroutine" in str(w.message).lower() and "await" in str(w.message).lower()
    ]
    assert not bad, f"Un-awaited coroutine warnings: {bad}"


@pytest.mark.asyncio
async def test_existing_polling_test_still_passes_via_payment_query():
    """Backwards-compat: even if some legacy tests stub query_selector
    instead of wait_for_function, the function still returns True via the
    payment signal (or via URL/selector). This test ensures we didn't accidentally
    drop the payment signal entirely."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"
    # All three waiters time out; the payment-element fallback is what triggers
    page.wait_for_selector = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    # New: wait_for_function for payment-visible JS resolves quickly
    page.wait_for_function = AsyncMock(side_effect=_waiter(MagicMock(), delay=0.01))
    page.query_selector = AsyncMock(return_value=MagicMock())  # legacy stub
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is True


@pytest.mark.asyncio
async def test_returns_on_payment_element_without_url_change():
    """06/26 P0b: Tock's hold/checkout can render as a URL-STABLE modal — the
    URL stays /search, so checkout_container CSS, the URL regex, AND the
    URL-gated payment-visible JS all miss it. A non-URL-gated payment-element
    waiter (saved card / CVC input) must still detect checkout."""
    from src.booker import _CHECKOUT_PAYMENT_SELECTOR
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/search"  # never changes

    async def sel_side_effect(selector, **kw):
        if selector == _CHECKOUT_PAYMENT_SELECTOR:
            return MagicMock()        # checkout payment UI is present
        raise _timeout_exc()          # checkout_container never matches

    page.wait_for_selector = AsyncMock(side_effect=sel_side_effect)
    page.wait_for_url = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.wait_for_function = AsyncMock(side_effect=_waiter(None, raise_exc=_timeout_exc()))
    page.query_selector = AsyncMock(return_value=None)
    page.screenshot = AsyncMock()

    result = await booker._wait_for_checkout(page, slot)
    assert result is True
