"""Tests for Cloudflare challenge detection during prewarm (Phase A+2 Task 3)."""
from datetime import date
from unittest.mock import AsyncMock, MagicMock
import pytest

from src.checker import AvailabilityChecker


def _make_checker():
    from src.config import Config
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


def _make_page(url: str):
    """Build a mock Playwright Page with the given URL.

    Used to test _is_cloudflare_challenge_page (URL-only detection) and
    the prewarm flow's CF-detection branch.
    """
    page = AsyncMock()
    page.url = url
    page.goto = AsyncMock()
    page.wait_for_selector = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value=False)
    page.close = AsyncMock()
    return page


def test_detects_challenge_in_url():
    """A page whose URL contains 'challenge' is detected as a CF challenge."""
    checker = _make_checker()
    page = _make_page("https://www.exploretock.com/challenge?ray=abc123")
    assert checker._is_cloudflare_challenge_page(page) is True


def test_detects_cf_chl_query_param():
    """A page whose URL contains '__cf_chl' is detected as a CF challenge."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/test/search?date=X&__cf_chl_tk=abc"
    )
    assert checker._is_cloudflare_challenge_page(page) is True


def test_normal_page_not_flagged():
    """A normal Tock search URL is NOT flagged as a CF challenge."""
    checker = _make_checker()
    page = _make_page(
        "https://www.exploretock.com/test/search?date=2026-05-01&size=2&time=17%3A00"
    )
    assert checker._is_cloudflare_challenge_page(page) is False


@pytest.mark.asyncio
async def test_prewarm_counts_cf_challenges():
    """prewarm_target_dates exposes a challenge count after running."""
    checker = _make_checker()
    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]

    page_count = [0]
    async def make_page():
        page_count[0] += 1
        # First page: redirected to challenge URL
        if page_count[0] == 1:
            return _make_page(
                "https://www.exploretock.com/challenge?ray=abc"
            )
        return _make_page("https://www.exploretock.com/test/search?date=X")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    cc = getattr(checker, "_last_prewarm_cf_challenges", None)
    assert cc == 1, f"Expected 1 CF challenge counted; got {cc}"


@pytest.mark.asyncio
async def test_challenged_pages_not_stored_in_sniper_pages():
    """A page that hit a CF challenge must NOT be parked in _sniper_pages —
    sniper-poll should fall back to a fresh goto() for that date."""
    checker = _make_checker()
    dates = [date(2026, 5, 1)]

    async def make_page():
        return _make_page("https://www.exploretock.com/challenge?ray=abc")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0)

    assert "2026-05-01" not in checker._sniper_pages, (
        "CF-challenged date must not have a parked page"
    )


@pytest.mark.asyncio
async def test_prewarm_alerts_when_challenge_rate_exceeds_threshold():
    """If CF challenge rate > 5%, notifier.cf_challenge_warning is called."""
    notifier = MagicMock()
    checker = _make_checker()

    dates = [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3), date(2026, 5, 4)]

    async def make_page():
        # All four challenged → 100% rate
        return _make_page("https://www.exploretock.com/challenge?ray=z")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0, notifier=notifier)

    notifier.cf_challenge_warning.assert_called_once()
    kwargs = notifier.cf_challenge_warning.call_args.kwargs
    args = notifier.cf_challenge_warning.call_args.args
    rate = kwargs.get("rate") if "rate" in kwargs else (args[0] if args else None)
    assert rate is not None and rate >= 0.05, (
        f"Rate must exceed 5% threshold; got {rate}"
    )


@pytest.mark.asyncio
async def test_no_alert_when_challenge_rate_below_threshold():
    """If CF challenge rate ≤ 5%, no Discord alert fires."""
    notifier = MagicMock()
    checker = _make_checker()

    # 1 challenge out of 25 = 4.0%, comfortably below the 5% threshold.
    # 25 still exceeds the production cap of 7, but is closer to a realistic
    # diagnostic batch a developer might use to verify alert suppression.
    dates = [date(2026, 5, i + 1) for i in range(25)]

    page_count = [0]
    async def make_page():
        page_count[0] += 1
        if page_count[0] == 1:
            return _make_page("https://www.exploretock.com/challenge?ray=z")
        return _make_page("https://www.exploretock.com/test/search?date=X")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0, notifier=notifier)

    notifier.cf_challenge_warning.assert_not_called()


@pytest.mark.asyncio
async def test_no_alert_at_exactly_5_percent_boundary():
    """Threshold is strict (> 0.05) — a rate of exactly 5.0% must NOT alert.

    Pins the boundary semantics so a future change from `>` to `>=` (or
    vice versa) is caught by the test suite.
    """
    notifier = MagicMock()
    checker = _make_checker()

    # 1 challenge in 20 dates = 5.0% exactly
    dates = [date(2026, 5, i + 1) for i in range(20)]

    page_count = [0]
    async def make_page():
        page_count[0] += 1
        if page_count[0] == 1:
            return _make_page("https://www.exploretock.com/challenge?ray=z")
        return _make_page("https://www.exploretock.com/test/search?date=X")
    checker.browser.new_page = make_page

    await checker.prewarm_target_dates(dates, stagger_sec=0, notifier=notifier)

    # rate == 0.05 exactly; threshold is strict `> 0.05` → NOT an alert
    notifier.cf_challenge_warning.assert_not_called()


# ---------------------------------------------------------------------------
# Codex pass 2: sniper-phase CF telemetry
# ---------------------------------------------------------------------------

def test_cf_challenge_warning_distinguishes_phase():
    """The Discord alert title differs for prewarm vs sniper phase."""
    from src.notifier import Notifier
    config = MagicMock()
    config.discord_webhook_url = ""  # Discord disabled — _fire is a no-op
    notifier = Notifier(config)
    fired = []
    notifier._fire = lambda **kwargs: fired.append(kwargs)

    notifier.cf_challenge_warning(rate=0.1, count=1, phase="prewarm")
    notifier.cf_challenge_warning(rate=0.5, count=2, phase="sniper")

    titles = [f["title"] for f in fired]
    assert any("Cloudflare Challenge Rate Elevated" in t for t in titles), (
        f"Expected prewarm title not found in: {titles}"
    )
    assert any("Sniper Window Challenged" in t for t in titles), (
        f"Expected sniper title not found in: {titles}"
    )
    assert any("URGENT" in t for t in titles), (
        f"Expected URGENT in sniper title not found in: {titles}"
    )


def test_sniper_cf_counters_initialize_to_zero():
    """AvailabilityChecker starts with _sniper_cf_challenges and
    _sniper_cf_attempts both zero."""
    checker = _make_checker()
    assert checker._sniper_cf_challenges == 0
    assert checker._sniper_cf_attempts == 0


@pytest.mark.asyncio
async def test_close_sniper_pages_resets_sniper_cf_counters():
    """close_sniper_pages() resets the sniper CF counters so they don't
    bleed across release windows."""
    checker = _make_checker()
    checker._sniper_cf_challenges = 3
    checker._sniper_cf_attempts = 5

    await checker.close_sniper_pages()

    assert checker._sniper_cf_challenges == 0
    assert checker._sniper_cf_attempts == 0
