"""Tests for exception handling in concurrent check_all()."""
import pytest
from datetime import date
from unittest.mock import AsyncMock, MagicMock
from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


def _make_config() -> Config:
    return Config(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=True,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=1,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )


class TestGatherExceptionLogging:
    @pytest.mark.asyncio
    async def test_exceptions_counted_in_last_errors(self):
        """Exceptions from gather should be counted in last_errors."""
        config = _make_config()
        checker = AvailabilityChecker(config, MagicMock(), MagicMock())

        call_count = 0
        async def _mock_check(d, keep_page=False, abort_event=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("Cloudflare blocked")
            return []

        checker._check_date = _mock_check
        checker._get_target_dates = lambda days=None, sniper_mode=False: [date(2026, 4, 17), date(2026, 4, 24)]

        await checker.check_all(concurrent=True)
        assert checker.last_errors >= 1

    @pytest.mark.asyncio
    async def test_valid_results_still_collected(self):
        """Even with exceptions, valid slot results should be collected."""
        config = _make_config()
        checker = AvailabilityChecker(config, MagicMock(), MagicMock())

        slot = AvailableSlot(slot_date=date(2026, 4, 24), slot_time="5:00 PM", day_of_week="Friday")
        call_count = 0
        async def _mock_check(d, keep_page=False, abort_event=None):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise TimeoutError("fail")
            return [slot]

        checker._check_date = _mock_check
        checker._get_target_dates = lambda days=None, sniper_mode=False: [date(2026, 4, 17), date(2026, 4, 24)]
        checker.tracker = MagicMock()
        checker.tracker.record = MagicMock(return_value=False)

        result = await checker.check_all(concurrent=True)
        assert len(result) == 1
        assert checker.last_errors >= 1
