"""Tests for sniper-mode scan-week cap (A4 fix).

Tock releases at most the next 2 weeks of slots. During sniper mode the
date list must be capped at sniper_scan_weeks regardless of the larger
normal-mode scan_weeks setting.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

from src.checker import AvailabilityChecker


def _make_checker(scan_weeks=4, sniper_scan_weeks=2):
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday", "Saturday", "Sunday"],
        fallback_days=[], preferred_time="17:00", scan_weeks=scan_weeks,
        dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        sniper_scan_weeks=sniper_scan_weeks,
    )
    return AvailabilityChecker(config, MagicMock(), MagicMock())


def test_normal_mode_uses_full_scan_weeks():
    """In normal mode, _get_target_dates returns dates up to scan_weeks weeks out."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    dates = checker._get_target_dates(["Friday"], sniper_mode=False)
    assert len(dates) >= 3, (
        f"4 weeks should yield ≥3 Fridays (depending on today); got {len(dates)}"
    )
    horizon = date.today() + timedelta(weeks=4)
    assert all(d <= horizon for d in dates)


def test_sniper_mode_caps_at_sniper_scan_weeks():
    """In sniper mode, _get_target_dates is capped at sniper_scan_weeks weeks out."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    dates = checker._get_target_dates(["Friday"], sniper_mode=True)
    horizon = date.today() + timedelta(weeks=2)
    assert all(d <= horizon for d in dates), (
        f"All dates must be within sniper_scan_weeks=2; got {dates}"
    )
    # Tight invariant: any 14-day window contains AT MOST 2 Fridays.
    # The loose <=3 bound used previously could not catch a regression
    # where sniper_scan_weeks and scan_weeks were accidentally swapped.
    assert len(dates) <= 2, (
        f"14-day window must contain ≤2 Fridays; got {len(dates)}: {dates}"
    )


def test_sniper_cap_smaller_than_normal():
    """Sniper-mode list must be a subset of normal-mode list when both same days."""
    checker = _make_checker(scan_weeks=4, sniper_scan_weeks=2)
    normal = set(checker._get_target_dates(["Friday"], sniper_mode=False))
    sniper = set(checker._get_target_dates(["Friday"], sniper_mode=True))
    assert sniper <= normal, (
        f"Sniper list must be a subset of normal list; "
        f"sniper={sniper}, normal={normal}"
    )
