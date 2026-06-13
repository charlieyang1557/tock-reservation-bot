"""Fix 3 (P2) — the booker's fresh-navigation search URL must seed time=
from the SLOT's own time, not the hardcoded config.preferred_time.

2026-06-12 incident: booker._book_single built every URL with
&time={config.preferred_time} (17:00), so the 8:00 PM slots navigated to
&time=17:00. Proven non-causal for that incident (the 5PM slots failed
identically) but a real correctness bug — fix so the seed matches the slot.
"""
from datetime import date

import pytest

from src.booker import _slot_time_to_24h
from src.checker import AvailableSlot


def _make_booker():
    from src.booker import TockBooker
    from src.config import Config
    from unittest.mock import MagicMock
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="fui-hui-hua",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    return TockBooker(config, MagicMock(), MagicMock())


def _slot(slot_time: str):
    return AvailableSlot(
        slot_date=date(2026, 6, 19), slot_time=slot_time, day_of_week="Friday",
    )


# --- the pure time converter ------------------------------------------------

@pytest.mark.parametrize("slot_time,expected", [
    ("5:00 PM", "17:00"),
    ("8:00 PM", "20:00"),
    ("11:30 AM", "11:30"),
    ("12:00 PM", "12:00"),   # noon
    ("12:00 AM", "00:00"),   # midnight
    ("9:45 PM", "21:45"),
])
def test_slot_time_to_24h_parses_am_pm(slot_time, expected):
    assert _slot_time_to_24h(slot_time, fallback="17:00") == expected


@pytest.mark.parametrize("slot_time,expected", [
    ("8 PM", "20:00"),     # hour-only label (no minutes) — must NOT fall back
    ("5 PM", "17:00"),
    ("12 AM", "00:00"),
    ("12 PM", "12:00"),
])
def test_slot_time_to_24h_parses_hour_only_label(slot_time, expected):
    # code-review: Source-1 span text is verbatim; '8 PM' must map to 20:00,
    # not silently fall back to preferred_time (the wrong-time regression).
    assert _slot_time_to_24h(slot_time, fallback="09:00") == expected


@pytest.mark.parametrize("slot_time,expected", [
    ("8:00PM", "20:00"),   # no space before AM/PM (concatenated span)
    ("8PM", "20:00"),      # hour-only, no space
    ("5:00pm", "17:00"),   # lowercase
    ("20:00", "20:00"),    # already 24-hour
    ("17:00", "17:00"),
])
def test_slot_time_to_24h_parses_compact_and_24h(slot_time, expected):
    # code-review/codex: '8:00PM', '8PM', '20:00' previously fell back to
    # preferred_time (wrong time). Must parse to the slot's real time.
    assert _slot_time_to_24h(slot_time, fallback="09:00") == expected


@pytest.mark.parametrize("bad", ["", "Slot 1", "soon", "25:00 PM", None])
def test_slot_time_to_24h_falls_back_on_unparseable(bad):
    assert _slot_time_to_24h(bad, fallback="17:00") == "17:00"


def test_slot_time_to_24h_logs_warning_on_fallback(caplog):
    """The fallback must NOT be silent — a future label-format regression
    that re-creates the wrong-time bug needs to be visible in the log."""
    import logging
    with caplog.at_level(logging.WARNING, logger="src.booker"):
        out = _slot_time_to_24h("half past seven", fallback="17:00")
    assert out == "17:00"
    assert any("half past seven" in r.getMessage() for r in caplog.records), (
        "expected a WARNING naming the unparseable slot time"
    )


# --- the URL build uses the slot's own time ---------------------------------

def test_build_search_url_uses_slot_time_for_8pm():
    booker = _make_booker()
    url = booker._build_search_url(_slot("8:00 PM"))
    assert "date=2026-06-19" in url
    assert "size=2" in url
    assert "time=20:00" in url          # the slot's time, NOT config 17:00
    assert "time=17:00" not in url


def test_build_search_url_uses_slot_time_for_5pm():
    booker = _make_booker()
    url = booker._build_search_url(_slot("5:00 PM"))
    assert "time=17:00" in url


def test_build_search_url_falls_back_to_preferred_time_when_slot_time_unparseable():
    booker = _make_booker()
    url = booker._build_search_url(_slot("Slot 1"))
    assert "time=17:00" in url          # config.preferred_time fallback
