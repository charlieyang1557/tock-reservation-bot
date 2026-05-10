"""
PagePool — pre-warmed Playwright pages for race overflow.

Phase B3.3 of the booking-speedups plan.

Why this exists
───────────────
``book_best_slot_race`` schedules N concurrent tasks. Each task that doesn't
have a warm page from the checker handoff calls ``browser.new_page()``
directly. Under high concurrency, this serializes on Chromium's process
model and adds 150–500 ms to the cold-task latency.

The pool keeps ``target_size`` blank pages pre-opened with stealth applied.
``acquire()`` returns one immediately if available; otherwise it falls
through to ``browser.new_page()``. After each acquire, a background
refill task lazily restocks the pool.

Design
──────
- Pool stored in a ``deque`` (FIFO, simple).
- Refill is fire-and-forget via ``asyncio.create_task``; the booker's hot
  path NEVER blocks waiting for a fresh page to launch.
- A semaphore caps concurrent refills so a sniper-mode burst can't open
  10+ pages at once and exhaust Chromium memory.
- ``release(page)`` ALWAYS closes the page — DOM is dirty after a race
  attempt and reusing it would risk stale state. Refill happens via the
  background task that was scheduled at acquire time.
- ``target_size = 0`` is a valid operator-side disable: ``start()`` is a
  no-op, ``acquire()`` always falls through, ``release()`` always closes.
"""

import asyncio
import logging
from collections import deque

from playwright.async_api import Page

logger = logging.getLogger(__name__)


# Default cap on concurrent refill tasks. The pool itself has a target size,
# but if the pool is drained N times in rapid succession, only this many
# refill operations may run in parallel — preventing a thundering-herd
# new_page() launch during sniper bursts.
DEFAULT_MAX_CONCURRENT_REFILLS = 2


class PagePool:
    """Bounded pool of pre-warmed Playwright pages with lazy refill."""

    def __init__(
        self,
        browser,  # TockBrowser, but avoid circular import
        target_size: int = 4,
        max_concurrent_refills: int = DEFAULT_MAX_CONCURRENT_REFILLS,
    ) -> None:
        self.browser = browser
        self.target_size = max(0, int(target_size))
        self.max_concurrent_refills = max(1, int(max_concurrent_refills))

        self._pool: deque[Page] = deque()
        # Cap concurrent refill tasks so a sniper burst doesn't spawn 10+
        # browser.new_page() calls simultaneously.
        self._refill_semaphore = asyncio.Semaphore(self.max_concurrent_refills)
        # Track outstanding refill tasks so close_all() can cancel them.
        self._refill_tasks: set[asyncio.Task] = set()
        self._closed = False

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def current_size(self) -> int:
        """Number of pre-warmed pages currently sitting in the pool."""
        return len(self._pool)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Pre-warm ``target_size`` pages. Safe to call once at browser startup.

        If ``target_size == 0``, this is a no-op. The operator has chosen
        to disable the pool.
        """
        if self.target_size == 0:
            logger.info("[pool] target_size=0 — page pool disabled.")
            return
        if self._closed:
            logger.warning("[pool] start() called after close_all() — ignoring.")
            return

        logger.info(f"[pool] Pre-warming {self.target_size} blank page(s)…")
        # Open pages in parallel — Chromium can handle batched new_page calls
        # at startup, and we want the bot ready as fast as possible.
        results = await asyncio.gather(
            *(self.browser.new_page() for _ in range(self.target_size)),
            return_exceptions=True,
        )
        for r in results:
            if isinstance(r, Exception):
                logger.warning(f"[pool] Pre-warm page failed: {r}")
                continue
            self._pool.append(r)
        logger.info(f"[pool] Ready with {self.current_size} page(s).")

    async def close_all(self) -> None:
        """Close every pooled page and cancel any in-flight refills.

        Idempotent — safe to call multiple times.
        """
        self._closed = True

        # Cancel any pending refill tasks first so they don't add a fresh
        # page to the pool after we drain it.
        for task in list(self._refill_tasks):
            if not task.done():
                task.cancel()
        if self._refill_tasks:
            await asyncio.gather(*self._refill_tasks, return_exceptions=True)
        self._refill_tasks.clear()

        # Now close every pooled page.
        while self._pool:
            page = self._pool.popleft()
            try:
                if not page.is_closed():
                    await page.close()
            except Exception as e:
                logger.debug(f"[pool] Page close during shutdown failed: {e}")

    # ------------------------------------------------------------------
    # Acquire / release
    # ------------------------------------------------------------------

    async def acquire(self) -> Page:
        """Return a pre-warmed page if available, else create a fresh one.

        On the hot path this MUST NOT block waiting for refill — sniper
        polls every 3 s, and we cannot stall for 200 ms.

        Raises whatever ``browser.new_page()`` raises if the pool is empty
        and creating a fresh page fails — callers must handle this.
        """
        if self._pool:
            page = self._pool.popleft()
            # Schedule a refill in the background so the next acquire is
            # also fast. Best-effort; failures are logged but not raised.
            self._schedule_refill()
            return page

        # Pool empty (or disabled). Caller owns the page lifecycle from here.
        return await self.browser.new_page()

    async def release(self, page: Page) -> None:
        """Close the page; the pool refills lazily via background tasks.

        The DOM state of a released page is dirty (a checkout attempt may
        have left half-rendered modal state, half-filled forms, error toasts).
        Reusing it would risk subtle bugs that only surface during a race,
        so we always close.

        Never raises — page-close failures are logged at debug level.
        """
        try:
            if not page.is_closed():
                await page.close()
        except Exception as e:
            logger.debug(f"[pool] release page.close() failed (non-critical): {e}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _schedule_refill(self) -> None:
        """Best-effort background task to refill the pool to target_size.

        Skipped if pool already at target, or if shutdown is in progress.
        Capped by ``_refill_semaphore`` so concurrent refills don't pile up.
        """
        if self._closed:
            return
        if self.target_size == 0:
            return
        if self.current_size >= self.target_size:
            return

        task = asyncio.create_task(self._refill_one())
        self._refill_tasks.add(task)
        # Auto-clean from the tracking set when done so it doesn't grow
        # unboundedly during a long sniper session.
        task.add_done_callback(self._refill_tasks.discard)

    async def _refill_one(self) -> None:
        """Open one page and add it to the pool, capped by the semaphore."""
        async with self._refill_semaphore:
            # Re-check after acquiring the semaphore — by the time we get
            # in, the pool may have already been refilled by a sibling task,
            # or shutdown may have started.
            if self._closed:
                return
            if self.current_size >= self.target_size:
                return

            try:
                page = await self.browser.new_page()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Background refill failures are best-effort — log and move on.
                # The next acquire() will simply fall through to new_page() if
                # the pool is still empty.
                logger.warning(f"[pool] Refill new_page() failed: {e}")
                return

            # Race check: another task may have raced us. Cap at target_size.
            if self._closed or self.current_size >= self.target_size:
                # Don't leak the page we just opened.
                try:
                    if not page.is_closed():
                        await page.close()
                except Exception:
                    pass
                return

            self._pool.append(page)

    async def wait_for_idle_refills(self) -> None:
        """Test helper: await all in-flight refill tasks to settle.

        Production code should never need this — the pool is designed to
        operate asynchronously. Tests use it to make refill behavior
        deterministic without time-based polling.
        """
        # Snapshot — the set may mutate as tasks complete and self-remove.
        tasks = list(self._refill_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
