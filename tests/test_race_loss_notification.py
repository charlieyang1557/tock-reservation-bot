"""Unit 4 (06/26 incident P1) — race-loss-SPECIFIC failure notification.

The RED Discord "Booking Failed" embed already fires (notifier.booking_failed),
but on 06/26 it said only the generic "slot vanished, checkout never loaded, or
payment prep failed". With the terminal-state detection (Unit 1) the booker now
knows WHY it failed; the monitor must surface that so the operator sees
"lost the race" / "sold out" instead of the catch-all.
"""
from unittest.mock import MagicMock

import pytest

from src.config import Config


def _make_booker():
    from src.booker import TockBooker
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return TockBooker(config, MagicMock(), MagicMock())


# --- describe_booking_failure (pure) ---------------------------------------

def test_describe_race_lost_is_specific():
    from src.monitor import describe_booking_failure
    msg = describe_booking_failure("race_lost").lower()
    assert "race" in msg or "someone else" in msg
    assert "booking_failures/" in describe_booking_failure("race_lost")


def test_describe_sold_out_is_specific():
    from src.monitor import describe_booking_failure
    assert "sold out" in describe_booking_failure("sold_out").lower()


def test_describe_none_is_generic_fallback():
    from src.monitor import describe_booking_failure
    msg = describe_booking_failure(None).lower()
    # Preserves the original generic wording so behavior is unchanged when the
    # failure cause is unknown.
    assert "checkout never loaded" in msg


# --- book_best_slot_race resets the reason each race ------------------------

@pytest.mark.asyncio
async def test_race_resets_terminal_reason_at_entry():
    """A stale reason from a prior race must not leak into the next race's
    failure notification."""
    booker = _make_booker()
    booker.last_terminal_reason = "race_lost"  # stale
    await booker.book_best_slot_race([])  # empty → FAILED, but resets first
    assert booker.last_terminal_reason is None
