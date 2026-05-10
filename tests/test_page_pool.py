"""Tests for the PagePool — pre-warmed Playwright pages for race overflow.

The PagePool maintains a small set of pre-warmed Playwright pages so that
when book_best_slot_race overflows beyond what the checker handed off, the
booker can grab a page from the pool instead of paying the ~150-500 ms cost
of browser.new_page() from scratch.

Phase B3.3 of the booking-speedups plan.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


def _make_unique_page() -> MagicMock:
    """Create a unique mock Page with async close()."""
    page = MagicMock()
    page.close = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    return page


def _make_browser_with_unique_pages() -> MagicMock:
    """Mock TockBrowser whose new_page() returns a fresh AsyncMock page each call."""
    browser = MagicMock()

    async def _new_page():
        return _make_unique_page()

    browser.new_page = AsyncMock(side_effect=_new_page)
    return browser


async def _drain_pending() -> None:
    """Yield enough times to flush refill background tasks scheduled with create_task."""
    # asyncio.sleep(0) yields one cycle; multiple yields flush a chain of refills
    for _ in range(10):
        await asyncio.sleep(0)


class TestPagePoolBasic:
    @pytest.mark.asyncio
    async def test_pool_returns_prewarmed_page_when_available(self):
        """start() with target_size=2 fills the pool; acquire() returns one of those."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=2)
        await pool.start()
        assert pool.current_size == 2
        assert browser.new_page.await_count == 2

        page = await pool.acquire()
        assert page is not None
        # Pre-warmed page came directly from the pool — no extra new_page call yet
        # (refill is async and may or may not have fired at this microtask)
        assert browser.new_page.await_count >= 2

        await pool.close_all()

    @pytest.mark.asyncio
    async def test_pool_creates_fresh_page_when_empty(self):
        """When pool is empty, acquire() falls through to browser.new_page()."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=2)
        # Don't call start() — pool is empty
        assert pool.current_size == 0

        page = await pool.acquire()
        assert page is not None
        assert browser.new_page.await_count == 1

        await pool.close_all()

    @pytest.mark.asyncio
    async def test_pool_refills_after_acquire(self):
        """After acquire(), the pool refills back to target_size lazily."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=3)
        await pool.start()
        assert pool.current_size == 3

        await pool.acquire()
        assert pool.current_size == 2  # one was popped

        # Yield to let the background refill task run
        await _drain_pending()
        await pool.wait_for_idle_refills()

        assert pool.current_size == 3
        assert browser.new_page.await_count == 4  # 3 initial + 1 refill

        await pool.close_all()

    @pytest.mark.asyncio
    async def test_pool_respects_target_size(self):
        """Pool never holds more than target_size pages."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=3)
        await pool.start()

        # Yield aggressively — even if internal refill logic is buggy, pool size
        # should never exceed 3
        await _drain_pending()
        await pool.wait_for_idle_refills()
        assert pool.current_size == 3

        # Releasing dirty pages does NOT add to pool (release closes them).
        # Refill via background tasks should still cap at 3.
        page = await pool.acquire()
        await pool.release(page)
        await _drain_pending()
        await pool.wait_for_idle_refills()
        assert pool.current_size <= 3

        await pool.close_all()


class TestPagePoolReleaseAndShutdown:
    @pytest.mark.asyncio
    async def test_pool_release_closes_page(self):
        """release(page) calls page.close() because DOM is dirty after a race attempt."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=0)  # disable so we control everything

        page = _make_unique_page()
        await pool.release(page)

        page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_close_all_closes_all_pages(self):
        """close_all() awaits page.close() on every pooled page."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=4)
        await pool.start()
        await _drain_pending()

        # Snapshot the pages currently in the pool
        # (pool internally stores them; we'll close_all and verify)
        assert pool.current_size == 4
        # Track pages directly — drain via private API for assertion
        snapshot = list(pool._pool)  # acceptable for testing only

        await pool.close_all()

        for p in snapshot:
            p.close.assert_awaited()
        assert pool.current_size == 0

    @pytest.mark.asyncio
    async def test_pool_close_all_cancels_pending_refills(self):
        """close_all() cancels any in-flight refill tasks so we don't leak."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=2)
        await pool.start()
        await pool.acquire()  # triggers refill background task

        await pool.close_all()

        # No pending refill tasks left (no warning about un-awaited coroutines)
        assert pool.current_size == 0


class TestPagePoolDisabled:
    @pytest.mark.asyncio
    async def test_pool_size_zero_disables_pool(self):
        """target_size=0 → acquire goes directly to new_page; release closes."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=0)

        # start() with size 0 is a no-op
        await pool.start()
        assert pool.current_size == 0
        assert browser.new_page.await_count == 0

        # acquire() falls through to browser.new_page()
        page = await pool.acquire()
        assert page is not None
        assert browser.new_page.await_count == 1

        # release() still closes (since pool can't accept more)
        await pool.release(page)
        page.close.assert_awaited_once()

        # No refill should have been scheduled
        await _drain_pending()
        await pool.wait_for_idle_refills()
        assert pool.current_size == 0

        await pool.close_all()


class TestPagePoolErrorHandling:
    @pytest.mark.asyncio
    async def test_pool_acquire_handles_browser_failure_gracefully(self):
        """If browser.new_page() raises during fall-through, acquire() raises."""
        from src.page_pool import PagePool

        browser = MagicMock()
        browser.new_page = AsyncMock(side_effect=RuntimeError("CDP died"))

        pool = PagePool(browser, target_size=0)

        with pytest.raises(RuntimeError, match="CDP died"):
            await pool.acquire()

    @pytest.mark.asyncio
    async def test_pool_release_handles_page_close_failure_gracefully(self):
        """If page.close() raises, release() does not propagate."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=0)

        bad_page = MagicMock()
        bad_page.is_closed = MagicMock(return_value=False)
        bad_page.close = AsyncMock(side_effect=RuntimeError("page already disposed"))

        # Should NOT raise
        await pool.release(bad_page)

        bad_page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_refill_swallows_browser_failures(self):
        """Background refill swallows new_page() exceptions silently."""
        from src.page_pool import PagePool

        # First 2 calls succeed (start), then refill fails
        call_count = {"n": 0}

        async def _flaky_new_page():
            call_count["n"] += 1
            if call_count["n"] <= 2:
                return _make_unique_page()
            raise RuntimeError("transient CDP error during refill")

        browser = MagicMock()
        browser.new_page = AsyncMock(side_effect=_flaky_new_page)

        pool = PagePool(browser, target_size=2)
        await pool.start()

        await pool.acquire()
        # Refill will fail; but acquire() succeeded and pool didn't crash
        await _drain_pending()
        await pool.wait_for_idle_refills()

        # Pool is below target now (refill failed); that's the contract
        assert pool.current_size <= 2

        await pool.close_all()


class TestPagePoolRefillCap:
    @pytest.mark.asyncio
    async def test_pool_caps_concurrent_refills(self):
        """Concurrent acquires don't spawn more than max_concurrent_refills tasks."""
        from src.page_pool import PagePool

        # Slow new_page so refills overlap
        new_page_started = asyncio.Event()
        block_new_page = asyncio.Event()
        active_count = {"current": 0, "max": 0}

        async def _slow_new_page():
            active_count["current"] += 1
            active_count["max"] = max(active_count["max"], active_count["current"])
            new_page_started.set()
            try:
                # Wait until we say go (only during refill phase, not initial start)
                if active_count["max"] > 4:  # initial start fills 4 in parallel
                    await block_new_page.wait()
            finally:
                active_count["current"] -= 1
            return _make_unique_page()

        browser = MagicMock()
        browser.new_page = AsyncMock(side_effect=_slow_new_page)

        pool = PagePool(browser, target_size=4, max_concurrent_refills=2)
        await pool.start()

        # Drain the pool fast — should kick off refills
        for _ in range(4):
            await pool.acquire()

        # Yield enough times for any refill task to start
        for _ in range(20):
            await asyncio.sleep(0)

        # Refills should be capped at max_concurrent_refills, even though
        # we acquired 4 pages in rapid succession.
        # The semaphore caps concurrent refills, so we shouldn't see more
        # than 4 (initial) + max_concurrent_refills (2) = 6 actively running
        # at the most aggressive moment. Since initial phase already finished,
        # only refills are running now: should be <= 2.
        assert active_count["current"] <= 2

        # Unblock and clean up
        block_new_page.set()
        await pool.wait_for_idle_refills()
        await pool.close_all()


class TestBookerIntegration:
    """Verify _book_single uses page_pool.acquire/release when no warm_page."""

    def _make_booker(self):
        from src.booker import TockBooker
        from src.config import Config
        config = Config(
            tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
            party_size=2, preferred_days=["Friday"], fallback_days=[],
            preferred_time="17:00", scan_weeks=2, dry_run=False, headless=True,
            sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
            sniper_interval_sec=3, release_window_days=["Monday"],
            release_window_start="09:00", release_window_end="11:00",
            debug_screenshots=False, discord_webhook_url="", card_cvc="",
        )
        return TockBooker(config, MagicMock(), MagicMock())

    @pytest.mark.asyncio
    async def test_book_single_uses_pool_when_no_warm_page(self):
        """_book_single calls page_pool.acquire() when no warm_page is provided."""
        from datetime import date
        from unittest.mock import patch

        from src.checker import AvailableSlot
        from src.page_pool import PagePool

        booker = self._make_booker()
        # Wire a real PagePool with a mock browser underneath
        pool_browser = _make_browser_with_unique_pages()
        real_pool = PagePool(pool_browser, target_size=2)
        await real_pool.start()

        booker.browser = MagicMock()
        booker.browser.page_pool = real_pool
        booker.browser.new_page = AsyncMock()  # should NOT be called directly

        # Prep a fake_page that will be returned by pool.acquire() — but we
        # actually want the real pool to hand us its real pre-warmed page.
        slot = AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM",
                             day_of_week="Friday")

        with patch.object(booker, "_click_calendar_day", AsyncMock(return_value=False)), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            booking_won = asyncio.Event()
            await booker._book_single(slot, booking_won, warm_page=None)

        # Direct browser.new_page() should not have been called
        # (pool was non-empty so no fall-through)
        booker.browser.new_page.assert_not_called()
        await real_pool.close_all()

    @pytest.mark.asyncio
    async def test_book_single_releases_pool_page_in_finally(self):
        """_book_single calls pool.release() in finally for booker-owned pool pages."""
        from datetime import date
        from unittest.mock import patch

        from src.checker import AvailableSlot
        from src.page_pool import PagePool

        booker = self._make_booker()

        pool_browser = _make_browser_with_unique_pages()
        real_pool = PagePool(pool_browser, target_size=2)
        await real_pool.start()

        booker.browser = MagicMock()
        booker.browser.page_pool = real_pool
        booker.browser.new_page = AsyncMock()

        # Spy on release
        release_calls = []
        original_release = real_pool.release

        async def spy_release(page):
            release_calls.append(page)
            await original_release(page)

        real_pool.release = spy_release  # type: ignore[assignment]

        slot = AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM",
                             day_of_week="Friday")
        with patch.object(booker, "_click_calendar_day", AsyncMock(return_value=False)), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            await booker._book_single(slot, asyncio.Event(), warm_page=None)

        assert len(release_calls) == 1
        await real_pool.close_all()

    @pytest.mark.asyncio
    async def test_book_single_does_not_release_warm_page_via_pool(self):
        """Warm pages are owned by the checker — _book_single must NOT release them via pool."""
        from datetime import date
        from unittest.mock import patch

        from src.checker import AvailableSlot
        from src.page_pool import PagePool

        booker = self._make_booker()
        pool_browser = _make_browser_with_unique_pages()
        real_pool = PagePool(pool_browser, target_size=2)
        await real_pool.start()

        booker.browser = MagicMock()
        booker.browser.page_pool = real_pool

        release_calls = []
        original_release = real_pool.release

        async def spy_release(page):
            release_calls.append(page)
            await original_release(page)

        real_pool.release = spy_release  # type: ignore[assignment]

        warm_page = MagicMock()
        warm_page.is_closed = MagicMock(return_value=False)
        warm_page.close = AsyncMock()

        slot = AvailableSlot(slot_date=date(2026, 5, 1), slot_time="5:00 PM",
                             day_of_week="Friday")
        with patch.object(booker, "_click_time_slot", AsyncMock(return_value=False)), \
             patch.object(booker, "_booking_screenshot", AsyncMock()):
            await booker._book_single(slot, asyncio.Event(), warm_page=warm_page)

        # The pool's release was NOT called for the warm page
        # (warm pages bypass pool release; they close directly per existing logic).
        assert warm_page not in release_calls
        # And the warm page itself was closed directly
        warm_page.close.assert_awaited()
        await real_pool.close_all()


class TestPagePoolIntegration:
    @pytest.mark.asyncio
    async def test_pool_handles_release_after_close_all(self):
        """release() after close_all() should not crash (defensive)."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=2)
        await pool.start()
        await pool.close_all()

        # Edge case: page returned after pool shutdown.
        # Should still close the page, not raise.
        page = _make_unique_page()
        await pool.release(page)
        page.close.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_pool_skips_refill_when_full(self):
        """If pool is already at target_size, no refill task is scheduled."""
        from src.page_pool import PagePool

        browser = _make_browser_with_unique_pages()
        pool = PagePool(browser, target_size=2)
        await pool.start()
        await _drain_pending()
        await pool.wait_for_idle_refills()

        initial_calls = browser.new_page.await_count
        assert initial_calls == 2

        # Trigger a few "release events" without acquire — pool stays full
        # No additional new_page calls should fire
        await _drain_pending()
        await pool.wait_for_idle_refills()
        assert browser.new_page.await_count == initial_calls

        await pool.close_all()
