"""Tests for src/calendar_replay.py parser pieces.

The parser converts Tock's protobuf calendar/full body bytes into
AvailableSlot objects. This is the safety-critical piece — a parser
bug could cause the bot to think slots exist when they don't (would
race against ghost slots and fail) or vice versa (would miss real
release moments — the 2026-06-05 incident).

Bodies are built with tests/proto_fixtures.py so they are structurally
valid wire-format protobuf in the real Tock shape (the parser is a
strict structural decoder since the 06/05 recalibration — byte soup no
longer parses). Ghost/edge coverage: test_replay_ghost_filter.py.
Real-capture coverage: test_replay_0605_release_capture.py.
"""
from datetime import date

from tests.proto_fixtures import calendar_body, date_section


def test_parse_finds_dates_with_times():
    """A body with one date + 3 booking-hour seatings → exactly 3 slots."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(date_section("2026-05-15", ["17:30", "18:00", "20:30"]))
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert {s.slot_time for s in slots} == {"5:30 PM", "6:00 PM", "8:30 PM"}
    assert all(s.slot_date == date(2026, 5, 15) for s in slots)
    assert all(s.day_of_week == "Friday" for s in slots)


def test_parse_filters_to_target_dates():
    """A body containing dates A and B; if only A is in target_dates,
    B's slots are dropped."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(
        date_section("2026-05-15", ["17:30"]),
        date_section("2026-05-22", ["18:00"]),
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert all(s.slot_date == date(2026, 5, 15) for s in slots)
    assert any(s.slot_time == "5:30 PM" for s in slots)


def test_parse_filters_out_implausible_times():
    """Only bookable hours (10:00–23:59) produce slots, even when the
    implausible times are structurally valid seating entries."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(
        date_section(
            "2026-05-15", ["00:00", "02:30", "08:00", "17:30", "21:00"]
        )
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert {s.slot_time for s in slots} == {"5:30 PM", "9:00 PM"}


def test_parse_deduplicates_repeated_times():
    """Tock's protobuf repeats date strings (and sometimes times).
    Each (date, time) pair should appear exactly once in output."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(
        date_section("2026-05-15", ["17:30", "17:30", "17:30", "20:00"])
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    times = {s.slot_time for s in slots}
    assert "5:30 PM" in times
    assert "8:00 PM" in times
    pair_counts: dict[tuple[str, str], int] = {}
    for s in slots:
        k = (s.slot_date.isoformat(), s.slot_time)
        pair_counts[k] = pair_counts.get(k, 0) + 1
    assert all(v == 1 for v in pair_counts.values()), (
        f"Each (date, time) must appear once; got {pair_counts}"
    )


def test_parse_returns_empty_for_empty_body():
    from src.calendar_replay import parse_available_slots
    assert parse_available_slots(b"", [date(2026, 5, 15)]) == []


def test_parse_returns_empty_when_no_dates_found():
    """A body with no date markers (e.g. a CF challenge HTML page)
    returns empty without raising."""
    from src.calendar_replay import parse_available_slots
    body = b"<!DOCTYPE html><html>Just a moment...</html>"
    assert parse_available_slots(body, [date(2026, 5, 15)]) == []


def test_parse_returns_empty_when_target_date_not_in_body():
    """If target_dates includes 2026-05-15 but the body only has
    2026-05-22, the result is empty."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(date_section("2026-05-22", ["17:30"]))
    assert parse_available_slots(body, [date(2026, 5, 15)]) == []


def test_parse_orders_slots_by_date_then_time():
    """Output is sorted: dates ascending, times ascending within each date."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(
        date_section("2026-05-22", ["20:00", "17:30"]),  # times out of order
        date_section("2026-05-15", ["18:00", "17:30"]),
    )
    slots = parse_available_slots(
        body, [date(2026, 5, 15), date(2026, 5, 22)]
    )
    # Verify date order: 5/15 first, 5/22 second
    by_date = []
    for s in slots:
        if not by_date or by_date[-1] != s.slot_date.isoformat():
            by_date.append(s.slot_date.isoformat())
    assert by_date == ["2026-05-15", "2026-05-22"]
    # Verify times sorted ASC within each date
    times_by_date: dict[str, list[str]] = {}
    for s in slots:
        times_by_date.setdefault(s.slot_date.isoformat(), []).append(s.slot_time)
    from src.calendar_replay import _time_sort_key
    for d, ts in times_by_date.items():
        assert ts == sorted(ts, key=_time_sort_key), (
            f"times for {d} not sorted: {ts}"
        )
    assert times_by_date["2026-05-15"] == ["5:30 PM", "6:00 PM"]
    assert times_by_date["2026-05-22"] == ["5:30 PM", "8:00 PM"]


def test_parse_handles_real_benu_body_shape():
    """Smoke test against the real benu calendar/full shape (per
    spikes/http_replay benu trace): high-volume date sections, each with
    the date string repeated at two nesting levels and many seatings."""
    from src.calendar_replay import parse_available_slots
    body = calendar_body(
        date_section("2026-05-13", ["17:30", "18:00", "18:30", "19:30"]),
        date_section("2026-05-24", ["17:30", "20:30"]),
    )
    slots = parse_available_slots(
        body, [date(2026, 5, 13), date(2026, 5, 24)]
    )
    times_by_date = {}
    for s in slots:
        times_by_date.setdefault(s.slot_date.isoformat(), set()).add(s.slot_time)
    assert times_by_date["2026-05-13"] == {"5:30 PM", "6:00 PM", "6:30 PM", "7:30 PM"}
    assert times_by_date["2026-05-24"] == {"5:30 PM", "8:30 PM"}


def test_format_24h_to_12h():
    """Time formatter handles AM, PM, noon, midnight conventions."""
    from src.calendar_replay import _format_24h_to_12h
    assert _format_24h_to_12h(0, 0) == "12:00 AM"
    assert _format_24h_to_12h(0, 30) == "12:30 AM"
    assert _format_24h_to_12h(11, 59) == "11:59 AM"
    assert _format_24h_to_12h(12, 0) == "12:00 PM"
    assert _format_24h_to_12h(12, 30) == "12:30 PM"
    assert _format_24h_to_12h(13, 0) == "1:00 PM"
    assert _format_24h_to_12h(17, 30) == "5:30 PM"
    assert _format_24h_to_12h(20, 0) == "8:00 PM"
    assert _format_24h_to_12h(23, 59) == "11:59 PM"


def test_time_sort_key_orders_correctly():
    """Time sort key produces ascending order across the booking range."""
    from src.calendar_replay import _time_sort_key
    times = ["5:00 PM", "10:00 AM", "12:00 PM", "8:30 PM", "11:30 AM"]
    assert sorted(times, key=_time_sort_key) == [
        "10:00 AM", "11:30 AM", "12:00 PM", "5:00 PM", "8:30 PM",
    ]
