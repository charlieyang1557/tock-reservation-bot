"""Tests for the Stripe-iframe URL cache in browser.find_in_frames
(Phase B2.3).

Tock embeds the CVC field inside a Stripe iframe whose URL contains
`stripe.com`. Today `find_in_frames` searches the main frame plus all
iframes in document order — the Stripe iframe usually isn't first, so
each CVC interaction pays an iterate-everything cost.

After this change, the first successful match per selector caches the
matching frame's URL prefix; subsequent calls scan matching frames
first, falling through to full scan on miss.

Saves: 75–400 ms per CVC interaction.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.browser import TockBrowser


def _make_browser():
    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    return TockBrowser(config)


def _frame(url: str, *, has_match: bool = False):
    """Build a Frame-like mock with a `url` attribute and a query_selector
    that returns a sentinel when has_match=True."""
    f = AsyncMock()
    f.url = url
    f.is_detached = MagicMock(return_value=False)
    f.query_selector = AsyncMock(
        return_value=MagicMock(name="found-element") if has_match else None
    )
    return f


def _page_with_frames(frames: list[AsyncMock]):
    """Page mock whose .main_frame is frames[0] and .frames is the full list."""
    page = MagicMock()
    page.main_frame = frames[0]
    page.frames = list(frames)
    return page


# ---------------------------------------------------------------------------
# Existing behavior: still finds across frames
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_in_frames_returns_first_match():
    """Sanity: still returns the first matching element across the
    main frame + iframes."""
    browser = _make_browser()
    main = _frame("https://www.exploretock.com/checkout", has_match=False)
    other = _frame("https://other.example/iframe", has_match=False)
    stripe = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page = _page_with_frames([main, other, stripe])

    el = await browser.find_in_frames(page, 'input[name="cvc"]')
    assert el is not None


# ---------------------------------------------------------------------------
# Cache invariants
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_caches_frame_url_pattern_after_first_match():
    """After the first successful find, the matching frame's URL prefix
    is cached on the browser instance for future calls."""
    browser = _make_browser()
    main = _frame("https://www.exploretock.com/checkout")
    stripe = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page = _page_with_frames([main, stripe])

    await browser.find_in_frames(page, 'input[name="cvc"]')

    # The cache should now contain a hint for this selector.
    cache = browser._frame_url_cache  # introduced by B2.3
    assert 'input[name="cvc"]' in cache
    assert "stripe.com" in cache['input[name="cvc"]']


@pytest.mark.asyncio
async def test_cached_frame_searched_first_on_subsequent_calls():
    """On a second call with the same selector, the cached frame is
    queried first — even if it appears LATER in `page.frames`."""
    browser = _make_browser()

    main = _frame("https://www.exploretock.com/checkout")
    other = _frame("https://other.example/iframe")
    stripe = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page = _page_with_frames([main, other, stripe])

    # Warm the cache
    await browser.find_in_frames(page, 'input[name="cvc"]')

    # Reset call counters on each frame so we can tell who was queried first
    main.query_selector.reset_mock()
    other.query_selector.reset_mock()
    stripe.query_selector.reset_mock()

    await browser.find_in_frames(page, 'input[name="cvc"]')

    # The cached (stripe) frame must have been queried at least once;
    # ideally it was queried BEFORE the others. The exact ordering is
    # what saves time — assert that cached-matching frames are consulted
    # before non-matching frames.
    assert stripe.query_selector.called
    # If the cache short-circuits the loop on success, main + other
    # should have been called 0 times on the second pass.
    assert main.query_selector.call_count == 0, (
        "Cached-matching frame should be tried first; main_frame should "
        "not have been re-queried"
    )
    assert other.query_selector.call_count == 0, (
        "Cached-matching frame should be tried first; non-matching "
        "iframes should not have been re-queried"
    )


@pytest.mark.asyncio
async def test_falls_through_to_full_scan_on_cache_miss():
    """If the cached frame URL no longer matches any frame (e.g., the
    Stripe iframe was unmounted and re-mounted at a different URL),
    fall back to the full scan."""
    browser = _make_browser()

    # Warm the cache with a stripe.com pattern via initial find
    main = _frame("https://www.exploretock.com/checkout")
    stripe1 = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page1 = _page_with_frames([main, stripe1])
    await browser.find_in_frames(page1, 'input[name="cvc"]')

    # Now construct a page where stripe.com is NOT present at all,
    # but a different frame matches
    main2 = _frame("https://www.exploretock.com/checkout")
    new_payment = _frame(
        "https://payments.example/cvc", has_match=True
    )
    page2 = _page_with_frames([main2, new_payment])

    el = await browser.find_in_frames(page2, 'input[name="cvc"]')
    assert el is not None  # still finds it via full scan


@pytest.mark.asyncio
async def test_cache_does_not_pollute_across_selectors():
    """Different selectors get separate cache entries — searching for
    `confirm_button` after a `cvc_input` hit must not reuse the Stripe
    URL pattern (which doesn't contain confirm buttons)."""
    browser = _make_browser()

    main = _frame("https://www.exploretock.com/checkout", has_match=False)
    stripe = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page = _page_with_frames([main, stripe])

    # Warm cache for cvc_input
    await browser.find_in_frames(page, 'input[name="cvc"]')

    # Now find a different selector whose match is on the main frame
    main.query_selector = AsyncMock(return_value=MagicMock(name="confirm"))
    stripe.query_selector = AsyncMock(return_value=None)

    await browser.find_in_frames(page, "button.confirm")

    # The cache for confirm should reflect the main frame URL — not
    # the Stripe pattern from the prior call.
    cache = browser._frame_url_cache
    assert "button.confirm" in cache
    assert "stripe.com" not in cache["button.confirm"]


@pytest.mark.asyncio
async def test_cache_skips_detached_frames():
    """If a cached-matching frame is now detached (frame.is_detached() is
    True), skip it during the cached-first pass."""
    browser = _make_browser()

    # Warm cache
    main = _frame("https://www.exploretock.com/checkout")
    stripe = _frame(
        "https://js.stripe.com/v3/elements-inner.html", has_match=True
    )
    page = _page_with_frames([main, stripe])
    await browser.find_in_frames(page, 'input[name="cvc"]')

    # Mark the stripe frame as detached for the second call
    stripe.is_detached = MagicMock(return_value=True)
    # New stripe-equivalent frame in a fresh page
    main2 = _frame("https://www.exploretock.com/checkout")
    fresh_stripe = _frame(
        "https://js.stripe.com/v3/elements-other.html", has_match=True
    )
    page2 = _page_with_frames([main2, fresh_stripe])
    # Ensure detached frame isn't reused — should still find on page2
    el = await browser.find_in_frames(page2, 'input[name="cvc"]')
    assert el is not None


@pytest.mark.asyncio
async def test_cache_does_not_block_on_no_match():
    """If the FIRST call yields no match anywhere, the cache stays empty
    and subsequent calls retry from scratch."""
    browser = _make_browser()

    main = _frame("https://www.exploretock.com/checkout", has_match=False)
    iframe = _frame("https://other.example/iframe", has_match=False)
    page = _page_with_frames([main, iframe])

    el = await browser.find_in_frames(page, 'input[name="cvc"]')
    assert el is None
    # Cache should NOT have been polluted with a phantom entry
    cache = browser._frame_url_cache
    assert 'input[name="cvc"]' not in cache
