"""Fix 2 (P1) — pre-warm a per-date page BEFORE the sniper window so the
FIRST post-release race hands the booker a page to reload() instead of
cold-navigating (the 2026-06-12 detect→click latency gap; the first poll is
the only one fast enough to win a Fuhuihua release).

Decoupled from sniper_reuse_pages: per-poll page REUSE stays OFF (the
2026-05-31 hydration-starvation finding), but a ONE-SHOT prewarmed handoff
page — reloaded exactly once by the booker via its existing replay-source
reload path — is parked in a dedicated _prewarm_pages dict the detection
scan never touches.
"""
from dataclasses import fields
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytz

from src.checker import AvailabilityChecker
from src.config import Config


def _config(**over):
    base = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    base.update(over)
    return Config(**base)


def _checker(**over):
    browser = MagicMock()
    browser.new_page = AsyncMock()
    return AvailabilityChecker(_config(**over), browser, MagicMock())


def _fake_page():
    p = AsyncMock()
    p.is_closed = MagicMock(return_value=False)
    p.evaluate = AsyncMock(return_value=False)  # is_cloudflare_challenge_page → False
    p.goto = AsyncMock()
    p.wait_for_selector = AsyncMock()
    p.close = AsyncMock()
    return p


# --- config ----------------------------------------------------------------

def test_sniper_prewarm_dates_defaults_true():
    for f in fields(Config):
        if f.name == "sniper_prewarm_dates":
            assert f.default is True
            return
    raise AssertionError("Config has no sniper_prewarm_dates field")


# --- core: park a handoff page with reuse OFF but prewarm ON ----------------

@pytest.mark.asyncio
async def test_prewarm_parks_handoff_page_when_reuse_off_prewarm_on():
    checker = _checker(sniper_reuse_pages=False, sniper_prewarm_dates=True)
    page = _fake_page()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates([date(2026, 6, 19)], stagger_sec=0)

    # NOT in _sniper_pages — the detection scan discards those with reuse off.
    assert "2026-06-19" not in checker._sniper_pages
    # Parked in the dedicated handoff dict, retrievable for the booker.
    assert checker.pop_prewarm_page("2026-06-19") is page
    page.close.assert_not_called()  # ownership handed off, not closed


@pytest.mark.asyncio
async def test_prewarm_does_not_park_when_both_off():
    """reuse off AND prewarm off → session warmed only, page closed (old behavior)."""
    checker = _checker(sniper_reuse_pages=False, sniper_prewarm_dates=False)
    page = _fake_page()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates([date(2026, 6, 19)], stagger_sec=0)

    assert checker._sniper_pages == {}
    assert checker.pop_prewarm_page("2026-06-19") is None
    page.close.assert_called()


@pytest.mark.asyncio
async def test_prewarm_reuse_on_still_parks_in_sniper_pages():
    """Regression guard: reuse-on path is unchanged (parks in _sniper_pages)."""
    checker = _checker(sniper_reuse_pages=True, sniper_prewarm_dates=True)
    page = _fake_page()
    checker.browser.new_page = AsyncMock(return_value=page)

    await checker.prewarm_target_dates([date(2026, 6, 19)], stagger_sec=0)

    assert checker._sniper_pages.get("2026-06-19") is page
    assert checker.pop_prewarm_page("2026-06-19") is None


# --- pop_prewarm_page ownership transfer -----------------------------------

def test_pop_prewarm_page_transfers_ownership():
    checker = _checker()
    page = _fake_page()
    checker._prewarm_pages["2026-06-19"] = page
    assert checker.pop_prewarm_page("2026-06-19") is page
    assert "2026-06-19" not in checker._prewarm_pages


def test_pop_prewarm_page_none_if_closed():
    checker = _checker()
    page = _fake_page()
    page.is_closed = MagicMock(return_value=True)
    checker._prewarm_pages["2026-06-19"] = page
    assert checker.pop_prewarm_page("2026-06-19") is None


def test_pop_prewarm_page_none_if_missing():
    checker = _checker()
    assert checker.pop_prewarm_page("2026-06-19") is None


# --- window-end cleanup closes prewarm pages -------------------------------

@pytest.mark.asyncio
async def test_close_sniper_pages_closes_prewarm_pages():
    checker = _checker()
    page = _fake_page()
    checker._prewarm_pages["2026-06-19"] = page
    await checker.close_sniper_pages()
    page.close.assert_called()
    assert checker._prewarm_pages == {}


# --- monitor gating + drain ------------------------------------------------

def _monitor(**over):
    from src.monitor import TockMonitor
    return TockMonitor(_config(**over), MagicMock(), MagicMock(), MagicMock(), MagicMock())


def _patch_now(monkeypatch):
    import src.monitor as monitor_mod
    PT = pytz.timezone("America/Los_Angeles")
    fake_now = PT.localize(datetime(2026, 5, 1, 19, 55))  # Friday, 4 min to 19:59
    fake_dt = MagicMock()
    fake_dt.now = MagicMock(return_value=fake_now)
    monkeypatch.setattr(monitor_mod, "datetime", fake_dt)


def test_should_prewarm_dates_fires_with_prewarm_on_reuse_off(monkeypatch):
    mon = _monitor(sniper_reuse_pages=False, sniper_prewarm_dates=True)
    _patch_now(monkeypatch)
    assert mon._should_prewarm_dates() == "Friday@19:59"


def test_should_prewarm_dates_none_when_both_off(monkeypatch):
    mon = _monitor(sniper_reuse_pages=False, sniper_prewarm_dates=False)
    _patch_now(monkeypatch)
    assert mon._should_prewarm_dates() is None


def test_seconds_until_prewarm_trigger_before_window():
    """Date-prewarm fires at window − 5 min; the trigger countdown must report
    the time until THAT, so the sleep can wake there (codex HIGH overrun fix)."""
    mon = _monitor(sniper_prewarm_dates=True, sniper_reuse_pages=False)
    mon._seconds_until_next_sniper = MagicMock(return_value=400.0)
    assert mon._seconds_until_prewarm_trigger() == 100.0  # 400 - 5*60


def test_seconds_until_prewarm_trigger_none_inside_window():
    mon = _monitor(sniper_prewarm_dates=True)
    mon._seconds_until_next_sniper = MagicMock(return_value=120.0)  # already < 5min
    assert mon._seconds_until_prewarm_trigger() is None


def test_seconds_until_prewarm_trigger_none_when_disabled():
    mon = _monitor(sniper_prewarm_dates=False, sniper_reuse_pages=False)
    mon._seconds_until_next_sniper = MagicMock(return_value=400.0)
    assert mon._seconds_until_prewarm_trigger() is None


def test_capped_sleep_wakes_at_prewarm_trigger_not_overshoot():
    """A coarse poll cadence must not overshoot the prewarm trigger and start a
    ~4min prewarm inside the window. The capped sleep wakes at the trigger."""
    mon = _monitor(sniper_prewarm_dates=True)
    mon._seconds_until_next_sniper = MagicMock(return_value=400.0)  # window 400s; trigger 100s
    secs, reason = mon._capped_sleep_seconds(900.0)
    assert secs == 100.0
    assert reason and "prewarm" in reason


def test_capped_sleep_wakes_at_window_once_past_trigger():
    mon = _monitor(sniper_prewarm_dates=True)
    mon._seconds_until_next_sniper = MagicMock(return_value=90.0)  # inside 5min, trigger gone
    secs, reason = mon._capped_sleep_seconds(900.0)
    assert secs == 90.0
    assert reason and "sniper" in reason


def test_prewarm_fits_budget():
    """The fit-guard: prewarm runs only if it can finish before the window.
    None / non-positive budget → skip (the SAFE direction, codex/code-review)."""
    mon = _monitor()
    # 7 dates → est = (7-1)*30 + 7*12 = 264s
    assert mon._prewarm_fits(7, 300.0) is True
    assert mon._prewarm_fits(7, 264.0) is True
    assert mon._prewarm_fits(7, 200.0) is False
    assert mon._prewarm_fits(7, None) is False    # unknown budget → skip
    assert mon._prewarm_fits(7, 0.0) is False
    assert mon._prewarm_fits(7, -5.0) is False
    assert mon._prewarm_fits(1, 20.0) is True      # 1 date → est = 0 + 12 = 12s


def test_drain_sniper_page_prefers_prewarm_when_no_warm():
    """The monitor's sniper drain hands the booker a prewarm page when no
    reuse warm page AND no fresh handoff page exists."""
    mon = _monitor()
    prewarm = _fake_page()
    mon.checker = MagicMock()
    mon.checker.pop_warm_page = MagicMock(return_value=None)
    mon.checker.pop_prewarm_page = MagicMock(return_value=prewarm)
    mon.checker.pop_handoff_page = MagicMock(return_value=None)
    assert mon._drain_sniper_page("2026-06-19") is prewarm


def test_drain_prefers_fresh_handoff_over_stale_prewarm():
    """Code-review CONFIRMED bug: when BOTH a fresh found-slot handoff page
    (just rendered, has the live Book button) AND a stale pre-release prewarm
    page exist for a date, the booker must get the HANDOFF page — not the
    stale prewarm DOM (which has no Book button and isn't reloaded for
    source='dom' slots). Handoff must win over prewarm."""
    mon = _monitor()
    handoff, prewarm = _fake_page(), _fake_page()
    mon.checker = MagicMock()
    mon.checker.pop_warm_page = MagicMock(return_value=None)
    mon.checker.pop_prewarm_page = MagicMock(return_value=prewarm)
    mon.checker.pop_handoff_page = MagicMock(return_value=handoff)
    assert mon._drain_sniper_page("2026-06-19") is handoff
