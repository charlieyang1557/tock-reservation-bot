"""Warmup cal_timeout for the first 2 sniper scans.

Symptom (5/22 8 PM Friday Fuhuihua release): the first sniper scan after
the pre-release gate expires drives all 6 pre-warmed pages through
``page.reload()`` concurrently at the peak of Tock's release traffic. With
the existing 5 s calendar wait, 9/14 dates timed out → 64 % error rate →
adaptive logic flipped to SEQUENTIAL on poll #1, costing us the most
critical seconds of the release. The same pattern showed up at 17:00.

Fix: the first 2 sniper-window scans get a 12 s calendar wait. By scan 3+
either the surge has eased or sequential mode is already engaged, and the
tighter 5 s budget keeps poll-rate up.

Tests pin the timeout schedule itself plus the counter lifecycle
(increment per real scan; do NOT increment during pre-release ticks; reset
between sniper windows).
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker
from src.config import Config


def _make_checker() -> AvailabilityChecker:
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return AvailabilityChecker(config, MagicMock(), MagicMock())


# ---------------------------------------------------------------------------
# _compute_cal_timeout — pure function of (keep_page, _sniper_scan_count)
# ---------------------------------------------------------------------------

def test_cal_timeout_normal_mode_is_15s():
    """Non-sniper polls keep the existing 15 s budget."""
    checker = _make_checker()
    assert checker._compute_cal_timeout(keep_page=False) == 15000


def test_cal_timeout_first_sniper_scan_is_12s():
    """Scan #1 (counter==1) gets the warmup budget."""
    checker = _make_checker()
    checker._sniper_scan_count = 1
    assert checker._compute_cal_timeout(keep_page=True) == 12000


def test_cal_timeout_second_sniper_scan_is_12s():
    """Scan #2 still gets the warmup budget — first reload after the warmup
    one can still be slow if Tock is queueing requests."""
    checker = _make_checker()
    checker._sniper_scan_count = 2
    assert checker._compute_cal_timeout(keep_page=True) == 12000


def test_cal_timeout_third_sniper_scan_drops_to_5s():
    """Scan #3 onwards uses the tight 5 s budget to keep poll-rate up."""
    checker = _make_checker()
    checker._sniper_scan_count = 3
    assert checker._compute_cal_timeout(keep_page=True) == 5000


def test_cal_timeout_zero_scan_count_treated_as_warmup():
    """Counter==0 (before first scan completes) should still use the warmup
    budget so the timing is right on the very first call inside scan #1."""
    checker = _make_checker()
    checker._sniper_scan_count = 0
    assert checker._compute_cal_timeout(keep_page=True) == 12000


# ---------------------------------------------------------------------------
# Counter lifecycle in check_all()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_release_does_not_increment_scan_count():
    """Pre-release polls (age < 60s) return without scanning; counter stays 0."""
    checker = _make_checker()
    with patch.object(checker, "_check_date", new_callable=AsyncMock):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=30.0,
        )
    assert checker._sniper_scan_count == 0


@pytest.mark.asyncio
async def test_post_release_increments_scan_count():
    """A real sniper scan increments the counter once (not per date)."""
    checker = _make_checker()
    with patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=61.0,
        )
    assert checker._sniper_scan_count == 1


@pytest.mark.asyncio
async def test_repeated_sniper_scans_keep_counting():
    """Each subsequent real scan adds 1 — warmup window closes after #2."""
    checker = _make_checker()
    with patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        for _ in range(4):
            await checker.check_all(
                concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
            )
    assert checker._sniper_scan_count == 4


@pytest.mark.asyncio
async def test_normal_mode_does_not_increment_sniper_counter():
    """Non-sniper check_all() must NOT touch the sniper counter."""
    checker = _make_checker()
    with patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        await checker.check_all(concurrent=False, keep_pages=False)
    assert checker._sniper_scan_count == 0


@pytest.mark.asyncio
async def test_close_sniper_pages_resets_counter():
    """When the sniper window ends, the counter resets so the next window
    starts fresh in warmup mode."""
    checker = _make_checker()
    checker._sniper_scan_count = 7
    await checker.close_sniper_pages()
    assert checker._sniper_scan_count == 0


# ---------------------------------------------------------------------------
# Codex MEDIUM: replay-success must not consume the page-scan warmup budget
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_success_does_not_burn_warmup_budget():
    """When USE_CALENDAR_REPLAY is on and the replay fast-path succeeds,
    no page reload happened. The 12s warmup budget exists to absorb the
    release-moment latency spike on the FIRST per-date page scan. If the
    counter ticks during replay-success polls, the first real legacy
    fallback gets the 5s steady-state budget — exactly the failure mode
    this change is meant to fix. Codex MEDIUM finding."""
    checker = _make_checker()
    checker.config.use_calendar_replay = True
    with patch.object(
        checker, "_try_calendar_replay",
        new_callable=AsyncMock, return_value=[],
    ), patch.object(
        checker, "_check_date", new_callable=AsyncMock,
    ) as mock_check_date:
        # Two replay-success polls — the legacy page path never ran
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
        mock_check_date.assert_not_called()
    assert checker._sniper_scan_count == 0, (
        f"Replay-success polls should not burn the page-scan warmup budget; "
        f"counter was {checker._sniper_scan_count}"
    )


@pytest.mark.asyncio
async def test_replay_success_then_fallback_gets_warmup_timeout():
    """End-to-end of Codex's MEDIUM scenario: replay succeeds for the
    first two polls, then fails on poll 3 and falls back to per-date
    scans. The first real per-date scan must still see the 12s warmup
    timeout, not the 5s steady-state one."""
    checker = _make_checker()
    checker.config.use_calendar_replay = True

    # First two polls: replay succeeds (returns empty list)
    with patch.object(
        checker, "_try_calendar_replay",
        new_callable=AsyncMock, return_value=[],
    ), patch.object(checker, "_check_date", new_callable=AsyncMock):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )

    # Counter must still be 0 — replay never touched a page
    assert checker._sniper_scan_count == 0

    # Third poll: replay fails (returns None) → legacy path runs
    with patch.object(
        checker, "_try_calendar_replay",
        new_callable=AsyncMock, return_value=None,
    ), patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )

    # Now the counter has ticked exactly once (this is the first real scan)
    assert checker._sniper_scan_count == 1
    # … and the calendar-wait budget for it must be the warmup 12s
    assert checker._compute_cal_timeout(keep_page=True) == 12000, (
        "First real page scan after replay fallback must still get the "
        "12s warmup; otherwise the release-spike timeout returns"
    )


@pytest.mark.asyncio
async def test_replay_disabled_path_increments_counter():
    """Sanity check: when replay is OFF, the per-date scan path runs and
    the counter ticks normally."""
    checker = _make_checker()
    checker.config.use_calendar_replay = False
    with patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
    assert checker._sniper_scan_count == 1


@pytest.mark.asyncio
async def test_scan_failure_does_not_advance_counter():
    """Review M-1: if the legacy per-date scan raises an unhandled
    exception (e.g. browser disconnect, CancelledError), the warmup
    counter must NOT advance. The counter exists to track scans that
    actually ran; an aborted scan should leave the next call with the
    same warmup budget so the user still gets two real warmup attempts.

    Sequential mode is used because asyncio.gather(..., return_exceptions=
    True) in concurrent mode swallows _check_date exceptions before they
    escape _scan_dates. Sequential is the path that actually propagates
    them, and it's reachable in production after an adaptive flip.
    """
    checker = _make_checker()

    class _ScanCrash(RuntimeError):
        pass

    with patch.object(
        checker, "_check_date", new_callable=AsyncMock,
        side_effect=_ScanCrash("simulated browser disconnect"),
    ):
        with pytest.raises(_ScanCrash):
            await checker.check_all(
                concurrent=False,   # sequential — exception propagates
                keep_pages=True,
                sniper_window_age_sec=120.0,
            )

    assert checker._sniper_scan_count == 0, (
        f"Aborted scan must not consume warmup budget; counter was "
        f"{checker._sniper_scan_count}"
    )


@pytest.mark.asyncio
async def test_replay_failure_increments_counter_via_fallback():
    """When replay returns None on the first sniper poll, the legacy
    per-date scan runs and the counter ticks for that scan — counts toward
    the warmup budget normally."""
    checker = _make_checker()
    checker.config.use_calendar_replay = True
    with patch.object(
        checker, "_try_calendar_replay",
        new_callable=AsyncMock, return_value=None,
    ), patch.object(
        checker, "_check_date", new_callable=AsyncMock, return_value=[],
    ):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0,
        )
    assert checker._sniper_scan_count == 1
