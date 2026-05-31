"""Sniper-mode calendar-load hardening (post-release safety-net robustness).

Context (2026-05-30 benu dry-run): the DOM availability scan timed out
loading ``div.ConsumerCalendar-month`` for 2/14 dates under HEADLESS=true
(``Page.wait_for_selector: Timeout 12000ms exceeded``). Since
fix/replay-parallel-capture (commit 0617661) made the DOM scan the
load-bearing SAFETY NET during the post-release sniper window — replay
returns empty 100% of the time for fuhuihua, so check_all falls through to
the DOM scan — every calendar-load timeout is a date we fail to check during
a release that sells out in seconds.

Hardening under test:
  1. In sniper mode, a first-attempt calendar_container timeout triggers
     EXACTLY ONE quick page.reload() retry (bounded — never a loop).
  2. The retry uses a short, dedicated budget (_SNIPER_CAL_RETRY_TIMEOUT_MS).
  3. A recovered timeout is NOT counted as an error (so it does not flip the
     adaptive concurrent→sequential switch).
  4. On timeout we capture diagnostics: page URL, whether a CF challenge
     element is present, warm-reload vs cold-goto, and a screenshot — so we
     can tell a CF interstitial from slow SPA hydration from a real error.
  5. A CF challenge at timeout SKIPS the reload retry (a reload won't clear
     it within the budget) and fails fast so the date gets a fresh page next
     poll.
  6. Normal (non-sniper) mode keeps the existing single-attempt behavior.
  7. _check_date wires keep_page→sniper_mode and the reuse flag→reused.
"""
import logging
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailabilityChecker
from src.config import Config

try:  # realistic timeout exception; code catches generic Exception anyway
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError
except Exception:  # pragma: no cover
    class PlaywrightTimeoutError(Exception):
        pass


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_checker(**overrides) -> AvailabilityChecker:
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    for k, v in overrides.items():
        setattr(config, k, v)
    return AvailabilityChecker(config, MagicMock(), MagicMock())


def _timeout(ms: int = 5000) -> PlaywrightTimeoutError:
    return PlaywrightTimeoutError(f"Page.wait_for_selector: Timeout {ms}ms exceeded")


def _make_page(
    *,
    url: str = "https://www.exploretock.com/benu/search?date=2026-06-04&size=2&time=17:00",
    wait_side_effect=None,
    wait_return=None,
    cf_dom: bool = False,
) -> AsyncMock:
    """Build a mock Playwright Page for _wait_for_calendar unit tests.

    wait_side_effect: list consumed by wait_for_selector across calls (an
    exception instance is raised, anything else is returned).
    cf_dom: value returned by page.evaluate (the CF DOM probe).
    """
    page = AsyncMock()
    page.url = url
    page.is_closed = MagicMock(return_value=False)
    if wait_side_effect is not None:
        page.wait_for_selector = AsyncMock(side_effect=list(wait_side_effect))
    else:
        page.wait_for_selector = AsyncMock(return_value=wait_return or MagicMock())
    page.reload = AsyncMock()
    page.goto = AsyncMock()
    page.screenshot = AsyncMock()
    page.evaluate = AsyncMock(return_value=cf_dom)
    page.close = AsyncMock()
    return page


# ---------------------------------------------------------------------------
# Constant sanity
# ---------------------------------------------------------------------------

def test_retry_timeout_constant_is_short():
    """The retry budget is a dedicated, short value (quick second try)."""
    checker = _make_checker()
    assert isinstance(checker._SNIPER_CAL_RETRY_TIMEOUT_MS, int)
    # Must be shorter than the normal 15s budget — it's a *quick* reload.
    assert 0 < checker._SNIPER_CAL_RETRY_TIMEOUT_MS <= 8000


# ---------------------------------------------------------------------------
# Core retry behavior
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sniper_timeout_triggers_single_reload_retry_and_recovers():
    """First wait times out, reload happens once, second wait succeeds → True."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), MagicMock()])

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is True
    page.reload.assert_awaited_once()
    assert page.wait_for_selector.await_count == 2


@pytest.mark.asyncio
async def test_sniper_retry_failure_returns_false_with_exactly_one_reload():
    """Both attempts time out → False, and reload fired EXACTLY once (no loop)."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), _timeout()])

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is False
    page.reload.assert_awaited_once()
    assert page.wait_for_selector.await_count == 2


@pytest.mark.asyncio
async def test_retry_reload_failure_returns_false():
    """If the reload itself raises (nav timeout), we fail fast → False, and we
    do not attempt a second wait."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), MagicMock()])
    page.reload = AsyncMock(side_effect=_timeout(8000))

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is False
    page.reload.assert_awaited_once()
    # only the initial wait ran; the post-reload wait was never reached
    assert page.wait_for_selector.await_count == 1


@pytest.mark.asyncio
async def test_retry_uses_short_dedicated_budget_not_original_timeout():
    """The post-reload wait uses _SNIPER_CAL_RETRY_TIMEOUT_MS, NOT the original
    (possibly 12s warmup) budget."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(12000), MagicMock()])

    await checker._wait_for_calendar(
        page, "2026-06-04", timeout=12000, sniper_mode=True
    )

    second = page.wait_for_selector.await_args_list[1]
    assert second.kwargs.get("timeout") == checker._SNIPER_CAL_RETRY_TIMEOUT_MS


@pytest.mark.asyncio
async def test_success_first_try_no_retry_no_diagnostic():
    """Happy path: first wait succeeds → True, no reload, no CF probe."""
    checker = _make_checker()
    page = _make_page()  # wait_for_selector succeeds

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is True
    page.reload.assert_not_called()
    page.evaluate.assert_not_called()  # diagnostic (CF probe) never runs
    assert page.wait_for_selector.await_count == 1


# ---------------------------------------------------------------------------
# Cloudflare-challenge fast-fail
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cf_challenge_at_timeout_skips_retry():
    """When the diagnostic finds a CF challenge, the reload retry is SKIPPED
    (a reload won't clear it in budget) — fail fast so the date falls back to
    a fresh page next poll."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), MagicMock()], cf_dom=True)

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is False
    page.reload.assert_not_called()
    assert page.wait_for_selector.await_count == 1


@pytest.mark.asyncio
async def test_cf_via_url_skips_retry():
    """CF detected via the URL signal (challenge path) also skips the retry."""
    checker = _make_checker()
    page = _make_page(
        url="https://www.exploretock.com/cdn-cgi/challenge-platform/...",
        wait_side_effect=[_timeout(), MagicMock()],
        cf_dom=False,
    )

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=5000, sniper_mode=True
    )

    assert ok is False
    page.reload.assert_not_called()


# ---------------------------------------------------------------------------
# Normal (non-sniper) mode unchanged
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_normal_mode_no_retry_single_attempt():
    """Non-sniper mode keeps the existing single-attempt behavior: a timeout
    returns False with NO reload and NO CF probe."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout()])

    ok = await checker._wait_for_calendar(
        page, "2026-06-04", timeout=15000, sniper_mode=False
    )

    assert ok is False
    page.reload.assert_not_called()
    page.evaluate.assert_not_called()
    assert page.wait_for_selector.await_count == 1


@pytest.mark.asyncio
async def test_default_call_is_normal_mode():
    """Calling without sniper_mode defaults to non-sniper (no retry)."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout()])

    ok = await checker._wait_for_calendar(page, "2026-06-04", timeout=15000)

    assert ok is False
    page.reload.assert_not_called()


# ---------------------------------------------------------------------------
# Diagnostics: URL + CF presence + nav kind + screenshot
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_diagnostic_logs_url_cf_and_nav_kind(caplog):
    """On a sniper timeout, a diagnostic line records the URL, the CF-challenge
    boolean, and warm-reload vs cold-goto."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), _timeout()], cf_dom=False)

    with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING, logger="src.checker"):
            await checker._wait_for_calendar(
                page, "2026-06-04", timeout=5000, sniper_mode=True, reused=True,
            )

    diag = [r.message for r in caplog.records if "cal-timeout-diag" in r.message]
    assert diag, "expected a [cal-timeout-diag] line on timeout"
    msg = diag[0]
    assert "2026-06-04" in msg          # date present
    assert "benu" in msg                # page URL present
    assert "cf_challenge=False" in msg  # CF classification present
    assert "warm-reload" in msg         # reused=True → warm-reload nav kind


@pytest.mark.asyncio
async def test_diagnostic_logs_cold_goto_for_fresh_page(caplog):
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), _timeout()])

    with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING, logger="src.checker"):
            await checker._wait_for_calendar(
                page, "2026-06-04", timeout=5000, sniper_mode=True, reused=False,
            )

    diag = [r.message for r in caplog.records if "cal-timeout-diag" in r.message]
    assert diag and "cold-goto" in diag[0]


@pytest.mark.asyncio
async def test_diagnostic_saves_screenshot_on_timeout():
    """A diagnostic screenshot is captured on a sniper timeout (forensics for a
    rare, critical failure) regardless of debug_screenshots."""
    checker = _make_checker(debug_screenshots=False)
    page = _make_page(wait_side_effect=[_timeout(), _timeout()])

    with patch.object(
        checker, "_save_error_screenshot", new_callable=AsyncMock
    ) as ss:
        await checker._wait_for_calendar(
            page, "2026-06-04", timeout=5000, sniper_mode=True
        )

    ss.assert_awaited()


@pytest.mark.asyncio
async def test_diagnostic_screenshot_capped_once_per_window():
    """The forensic screenshot is heavy and lands in the never-pruned errors/
    dir, so it is capped to ONCE per sniper window (a real 11-min window can
    run dozens of DOM scans). The per-timeout LOG line is NOT capped — it
    stays one-per-timeout for full per-date detail. close_sniper_pages()
    (window boundary) re-arms the screenshot."""
    checker = _make_checker()

    with patch.object(
        checker, "_save_error_screenshot", new_callable=AsyncMock
    ) as ss:
        # Two timeouts in the same window → only the first screenshots.
        for _ in range(2):
            page = _make_page(wait_side_effect=[_timeout(), _timeout()])
            await checker._wait_for_calendar(
                page, "2026-06-04", timeout=5000, sniper_mode=True
            )
        assert ss.await_count == 1, "screenshot must be capped to once per window"

        # New window → screenshot re-armed.
        await checker.close_sniper_pages()
        page = _make_page(wait_side_effect=[_timeout(), _timeout()])
        await checker._wait_for_calendar(
            page, "2026-06-04", timeout=5000, sniper_mode=True
        )
        assert ss.await_count == 2, "close_sniper_pages must re-arm the screenshot"


@pytest.mark.asyncio
async def test_diagnostic_log_line_not_capped(caplog):
    """Even though the screenshot is capped, every timeout still emits a
    [cal-timeout-diag] line (cheap, and the per-date detail is the signal)."""
    checker = _make_checker()

    with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
        with caplog.at_level(logging.WARNING, logger="src.checker"):
            for d in ("2026-06-04", "2026-06-10"):
                page = _make_page(wait_side_effect=[_timeout(), _timeout()])
                await checker._wait_for_calendar(
                    page, d, timeout=5000, sniper_mode=True
                )

    diag = [r.message for r in caplog.records if "cal-timeout-diag" in r.message]
    assert len(diag) == 2
    assert any("2026-06-04" in m for m in diag)
    assert any("2026-06-10" in m for m in diag)


@pytest.mark.asyncio
async def test_diagnostic_probes_cloudflare():
    """The diagnostic calls the combined URL+DOM CF check (page.evaluate is the
    DOM half)."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), _timeout()], cf_dom=False)

    with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
        await checker._wait_for_calendar(
            page, "2026-06-04", timeout=5000, sniper_mode=True
        )

    page.evaluate.assert_awaited()  # CF DOM probe ran


@pytest.mark.asyncio
async def test_final_failure_still_logs_selector_failed(caplog):
    """After the retry also fails, the canonical SELECTOR_FAILED line still
    fires (monitoring/log-greps depend on it)."""
    checker = _make_checker()
    page = _make_page(wait_side_effect=[_timeout(), _timeout()])

    with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
        with caplog.at_level(logging.ERROR, logger="src.checker"):
            await checker._wait_for_calendar(
                page, "2026-06-04", timeout=5000, sniper_mode=True
            )

    assert any(
        "SELECTOR_FAILED" in r.message and "calendar_container" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Error-counting integration: recovered timeout must NOT inflate last_errors
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recovered_timeout_not_counted_as_error():
    """A transient timeout recovered by the retry must NOT increment
    last_errors — otherwise it could flip the adaptive switch to sequential."""
    checker = _make_checker(use_calendar_replay=False)
    target = date.today() + timedelta(days=14)
    checker._get_target_dates = (  # type: ignore[method-assign]
        lambda days, sniper_mode=False: [target] if "Friday" in days else []
    )

    async def fake_check_date(target_date, **kwargs):
        page = _make_page(wait_side_effect=[_timeout(), MagicMock()])
        with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
            await checker._wait_for_calendar(
                page, target_date.isoformat(), timeout=5000, sniper_mode=True
            )
        return []

    with patch.object(checker, "_check_date", side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0
        )

    assert checker.last_errors == 0
    assert checker.last_checks == 1


@pytest.mark.asyncio
async def test_unrecovered_timeout_counted_as_error():
    """A timeout that survives the retry IS counted (so the adaptive logic
    still reacts to genuine calendar-load failure)."""
    checker = _make_checker(use_calendar_replay=False)
    target = date.today() + timedelta(days=14)
    checker._get_target_dates = (  # type: ignore[method-assign]
        lambda days, sniper_mode=False: [target] if "Friday" in days else []
    )

    async def fake_check_date(target_date, **kwargs):
        page = _make_page(wait_side_effect=[_timeout(), _timeout()])
        with patch.object(checker, "_save_error_screenshot", new_callable=AsyncMock):
            await checker._wait_for_calendar(
                page, target_date.isoformat(), timeout=5000, sniper_mode=True
            )
        return []

    with patch.object(checker, "_check_date", side_effect=fake_check_date):
        await checker.check_all(
            concurrent=True, keep_pages=True, sniper_window_age_sec=120.0
        )

    assert checker.last_errors == 1


# ---------------------------------------------------------------------------
# _check_date wires the flags through to _wait_for_calendar
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_date_cold_page_passes_sniper_true_reused_false():
    checker = _make_checker()
    page = _make_page(
        url="https://www.exploretock.com/benu/search?date=2026-06-04&size=2&time=17:00",
    )
    checker.browser.new_page = AsyncMock(return_value=page)
    # calendar "fails" → _check_date returns [] right after the wait call.
    spy = AsyncMock(return_value=False)

    with patch.object(checker, "_wait_for_calendar", spy):
        await checker._check_date(date(2026, 6, 4), keep_page=True)

    spy.assert_awaited_once()
    _, kwargs = spy.await_args
    assert kwargs.get("sniper_mode") is True
    assert kwargs.get("reused") is False


@pytest.mark.asyncio
async def test_check_date_warm_reuse_passes_reused_true():
    checker = _make_checker(sniper_reuse_pages=True)  # exercises the reuse path
    date_str = "2026-06-04"
    warm = _make_page(
        url=f"https://www.exploretock.com/benu/search?date={date_str}&size=2&time=17:00",
    )
    checker._sniper_pages[date_str] = warm
    spy = AsyncMock(return_value=False)

    with patch.object(checker, "_wait_for_calendar", spy):
        await checker._check_date(date(2026, 6, 4), keep_page=True)

    warm.reload.assert_awaited_once()
    _, kwargs = spy.await_args
    assert kwargs.get("sniper_mode") is True
    assert kwargs.get("reused") is True


@pytest.mark.asyncio
async def test_check_date_normal_mode_passes_sniper_false():
    checker = _make_checker()
    page = _make_page(
        url="https://www.exploretock.com/benu/search?date=2026-06-04&size=2&time=17:00",
    )
    checker.browser.new_page = AsyncMock(return_value=page)
    spy = AsyncMock(return_value=False)

    with patch.object(checker, "_wait_for_calendar", spy):
        await checker._check_date(date(2026, 6, 4), keep_page=False, bypass_normal_skip=True)

    _, kwargs = spy.await_args
    assert kwargs.get("sniper_mode") is False
