"""Tests for stale-by-date expiry of booking_uncertain.json (Phase B2.1).

Old behavior: a stuck `booking_uncertain.json` would block ALL future
races indefinitely. If the operator forgot to clear the file after
verifying on Tock, the bot was effectively dead until next manual `rm`.

New behavior: when read_uncertain() finds a slot_date_str that's more
than 7 days in the past, it logs a warning, archives the file to
`booking_uncertain.archive/<timestamp>.json`, and returns None. Future
races proceed normally; the archive preserves the audit trail.
"""
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from src.booking_uncertain import (
    UncertainBooking,
    read_uncertain,
    write_uncertain,
    clear_uncertain,
)


def _make(slot_date: date) -> UncertainBooking:
    return UncertainBooking(
        slot_date_str=slot_date.isoformat(),
        slot_time="5:00 PM",
        day_of_week=slot_date.strftime("%A"),
        detected_at_iso=datetime.now().isoformat(),
    )


def test_read_uncertain_keeps_recent_file(tmp_path):
    """A file whose slot_date is within the past 7 days is preserved
    and returned."""
    f = tmp_path / "booking_uncertain.json"
    recent = date.today() - timedelta(days=2)
    write_uncertain(_make(recent), path=f)

    got = read_uncertain(path=f)
    assert got is not None
    assert got.slot_date_str == recent.isoformat()
    assert f.exists(), "Recent file must NOT be archived"


def test_read_uncertain_keeps_future_file(tmp_path):
    """A file whose slot_date is in the future (the booking hasn't
    happened yet) is also preserved — only past-by-7-days are stale."""
    f = tmp_path / "booking_uncertain.json"
    future = date.today() + timedelta(days=14)
    write_uncertain(_make(future), path=f)

    got = read_uncertain(path=f)
    assert got is not None
    assert f.exists()


def test_read_uncertain_returns_none_for_stale_date(tmp_path, caplog):
    """A file whose slot_date is more than 7 days in the past is treated
    as stale: read_uncertain returns None and logs a warning."""
    import logging
    f = tmp_path / "booking_uncertain.json"
    stale = date.today() - timedelta(days=10)
    write_uncertain(_make(stale), path=f)

    with caplog.at_level(logging.WARNING):
        got = read_uncertain(path=f)
    assert got is None, (
        f"Stale (>7d past) file must return None so future races aren't "
        f"blocked indefinitely; got {got}"
    )
    assert any(
        "stale" in rec.message.lower() or "archived" in rec.message.lower()
        for rec in caplog.records
    ), f"Expected a stale/archived warning; got: {[r.message for r in caplog.records]}"


def test_read_uncertain_archives_stale_file(tmp_path):
    """When stale, the file is moved into a `booking_uncertain.archive/`
    sibling directory with a timestamp suffix — original removed, audit
    trail preserved."""
    f = tmp_path / "booking_uncertain.json"
    archive_dir = tmp_path / "booking_uncertain.archive"
    stale = date.today() - timedelta(days=10)
    write_uncertain(_make(stale), path=f)

    assert f.exists()
    read_uncertain(path=f)

    assert not f.exists(), (
        "Stale file must be moved out of the live path so the bot "
        "stops checking it every cycle"
    )
    assert archive_dir.exists(), "Archive directory must be created"
    archived = list(archive_dir.glob("*.json"))
    assert len(archived) == 1, (
        f"Exactly one archived file expected; found {archived}"
    )
    # Archived filename should include a timestamp + the original name pattern
    name = archived[0].name
    assert "booking_uncertain" in name


def test_read_uncertain_handles_invalid_date_string(tmp_path, caplog):
    """If slot_date_str is malformed, treat as stale (return None +
    archive) rather than crashing — defensive programming."""
    import logging
    f = tmp_path / "booking_uncertain.json"
    f.write_text(
        '{"slot_date_str": "not-a-date", "slot_time": "5:00 PM",'
        ' "day_of_week": "Friday", "detected_at_iso": "2026-05-09T19:00:00"}'
    )

    with caplog.at_level(logging.WARNING):
        got = read_uncertain(path=f)
    assert got is None
    # File should be removed/archived to prevent infinite loop on a
    # malformed file that can never be parsed
    archive_dir = tmp_path / "booking_uncertain.archive"
    assert not f.exists() or archive_dir.exists()


def test_read_uncertain_returns_none_when_file_missing(tmp_path):
    """No file present → None (existing behavior preserved)."""
    f = tmp_path / "booking_uncertain.json"
    assert read_uncertain(path=f) is None


def test_archive_does_not_overwrite_existing_archives(tmp_path):
    """Two stale files archived back-to-back must produce two distinct
    archive entries (timestamp differentiates them)."""
    f = tmp_path / "booking_uncertain.json"
    archive_dir = tmp_path / "booking_uncertain.archive"
    stale = date.today() - timedelta(days=10)

    write_uncertain(_make(stale), path=f)
    read_uncertain(path=f)

    write_uncertain(_make(stale), path=f)
    read_uncertain(path=f)

    archived = list(archive_dir.glob("*.json"))
    assert len(archived) == 2, (
        f"Two distinct archives expected; got {[a.name for a in archived]}"
    )


@pytest.fixture(autouse=True)
def _cleanup(tmp_path):
    """Each test runs in its own tmp_path so file state can't leak."""
    yield
    # tmp_path is auto-cleaned by pytest
