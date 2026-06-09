"""Regression tests for the 2026-06-05 20:00 PT Fuhuihua release miss.

The calendar-replay fast-path FILTERED ALL 8 date sections of the real
release body (bot.log:256945 — "[replay-diag] body_len=3166 date_hits=30
unique_dates=8 sections_passed=0 sections_filtered=8"), costing ~9s of
detection latency while the DOM safety-net scan caught up.

Root cause: the ghost-slot guards (_MIN_BOOKABLE_SECTION_BYTES=800,
_MIN_BOOKABLE_TIMES_PER_SECTION=5) were calibrated on benu's high-volume
shape (1000-1500 b/section, 11-17 seatings). The real Fuhuihua release
had ~180-400 byte sections with 1-2 seatings each.

These tests run against the EXACT captured bytes
(tests/fixtures/20260605T200009_…_replay.bin, dumped by the replay-miss
calibration path that night). Ground truth, established by a strict
protobuf wire decode of the capture:

    2026-06-06  17:00            2026-06-12  17:00
    2026-06-07  17:00, 20:00     2026-06-13  17:00
    2026-06-11  18:30            (06-05, 06-10, 06-14: zero-length f2 → empty)

The capture also embeds the classic ghost traps: a 1441-byte prose
description, a date-range echo listing all 8 dates with no seatings,
and a "2026-06-05T19:59:56" timestamp string — none may produce slots.
"""
import pathlib
from datetime import date

import pytest

FIXTURE = (
    pathlib.Path(__file__).parent
    / "fixtures"
    / "20260605T200009_780186_fui-hui-hua-san-francisco_replay.bin"
)

ALL_CAPTURE_DATES = [
    date(2026, 6, 5), date(2026, 6, 6), date(2026, 6, 7), date(2026, 6, 10),
    date(2026, 6, 11), date(2026, 6, 12), date(2026, 6, 13), date(2026, 6, 14),
]


@pytest.fixture()
def release_body() -> bytes:
    body = FIXTURE.read_bytes()
    assert len(body) == 3166, "fixture must be the exact captured release body"
    return body


def test_release_capture_parses_preferred_dates_as_bookable(release_body):
    """THE incident regression: 06-12 (Fri) and 06-13 (Sat) — the two slots
    the DOM scan found at 20:00:09 — must come out of the replay parser."""
    from src.calendar_replay import parse_available_slots

    slots = parse_available_slots(
        release_body, [date(2026, 6, 12), date(2026, 6, 13)]
    )
    found = {(s.slot_date_str, s.slot_time) for s in slots}
    assert ("2026-06-12", "5:00 PM") in found, (
        f"replay must detect the real 06-12 release seating; got {found}"
    )
    assert ("2026-06-13", "5:00 PM") in found, (
        f"replay must detect the real 06-13 release seating; got {found}"
    )


def test_release_capture_finds_thursday_0611(release_body):
    """06-11 (Thursday) was in the release body @ 18:30 but the DOM scan
    never checked Thursdays (open issue from the incident memo). The
    replay parser sees the whole calendar — it must surface it."""
    from src.calendar_replay import parse_available_slots

    slots = parse_available_slots(release_body, [date(2026, 6, 11)])
    assert [(s.slot_date_str, s.slot_time, s.day_of_week) for s in slots] == [
        ("2026-06-11", "6:30 PM", "Thursday")
    ]


def test_release_capture_full_inventory_exact(release_body):
    """With every calendar date as a target, the parse must return exactly
    the 6 real seatings — nothing from the empty dates, the prose
    description, the date-range echo, or the trailing timestamp."""
    from src.calendar_replay import parse_available_slots

    slots = parse_available_slots(release_body, ALL_CAPTURE_DATES)
    found = {(s.slot_date_str, s.slot_time) for s in slots}
    assert found == {
        ("2026-06-06", "5:00 PM"),
        ("2026-06-07", "5:00 PM"),
        ("2026-06-07", "8:00 PM"),
        ("2026-06-11", "6:30 PM"),
        ("2026-06-12", "5:00 PM"),
        ("2026-06-13", "5:00 PM"),
    }
    assert len(slots) == 6, "exactly one AvailableSlot per real seating"


def test_release_capture_empty_dates_stay_empty(release_body):
    """The capture's zero-availability dates (f2 is zero-length) must not
    ghost — even though their date strings appear in the body 2-4 times
    (section + range echo + trailer)."""
    from src.calendar_replay import parse_available_slots

    slots = parse_available_slots(
        release_body, [date(2026, 6, 5), date(2026, 6, 10), date(2026, 6, 14)]
    )
    assert slots == [], f"empty dates must yield no slots; got {slots}"


def test_release_capture_diagnostics(release_body):
    """The diag counters that flagged the incident must now show 5 passed /
    3 filtered (5 dates with real seatings, 3 released-but-empty dates),
    with raw byte-scan counters unchanged from the incident log line."""
    from src.calendar_replay import parse_with_diagnostics

    slots, diag = parse_with_diagnostics(release_body, ALL_CAPTURE_DATES)
    assert diag.body_len == 3166
    assert diag.date_hits == 30
    assert diag.unique_dates == 8
    assert diag.sections_passed == 5, (
        f"5 dates carry real seatings; diag says {diag.sections_passed} "
        f"(incident value was 0)"
    )
    assert diag.sections_filtered == 3
    assert len(slots) == 6


def test_empty_calendar_tiny_body_is_negative():
    """Negative case from the same night (bot.log 20:15:15 — the re-captured
    calendar/full body was 4 bytes once the calendar emptied): a tiny body
    must never parse as bookable, and the protobuf plausibility gate must
    reject it so fetch_calendar() falls back instead of reporting success."""
    from src.calendar_replay import body_looks_protobuf, parse_available_slots

    tiny = b"\x0a\x02\x08\x02"  # 4-byte protobuf-framed empty calendar
    assert body_looks_protobuf(tiny) is False
    assert parse_available_slots(tiny, ALL_CAPTURE_DATES) == []


def test_release_capture_survives_per_date_cap(release_body):
    """End-to-end through cap_slots_per_date (as _try_calendar_replay
    applies it): the sparse release must pass the cap untouched — capping
    exists for benu's 100+ slot bodies, not for 1-seating releases."""
    from src.calendar_replay import cap_slots_per_date, parse_available_slots

    slots = parse_available_slots(release_body, ALL_CAPTURE_DATES)
    capped = cap_slots_per_date(slots, preferred_time="17:00", per_date_cap=5)
    assert {(s.slot_date_str, s.slot_time) for s in capped} == {
        (s.slot_date_str, s.slot_time) for s in slots
    }
