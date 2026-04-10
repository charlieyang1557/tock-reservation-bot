"""Tests for skip-date cache behavior during sniper mode."""
import pytest
from datetime import date
from unittest.mock import MagicMock
from src.checker import AvailabilityChecker
from src.config import Config


def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )
    defaults.update(overrides)
    return Config(**defaults)


class TestSkipDateCache:
    def test_clear_skip_cache(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._skip_dates.add("2026-04-17")
        checker.clear_skip_cache()
        assert "2026-04-17" not in checker._skip_dates

    def test_should_skip_when_enabled(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._skip_dates.add("2026-04-17")
        assert checker._should_skip_date("2026-04-17", skip_cache_enabled=True) is True

    def test_should_not_skip_when_disabled(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        checker._skip_dates.add("2026-04-17")
        assert checker._should_skip_date("2026-04-17", skip_cache_enabled=False) is False

    def test_should_not_skip_unknown_date(self) -> None:
        checker = AvailabilityChecker(_make_config(), MagicMock(), MagicMock())
        assert checker._should_skip_date("2026-04-17", skip_cache_enabled=True) is False
