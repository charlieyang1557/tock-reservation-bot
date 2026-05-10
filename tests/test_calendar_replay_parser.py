"""Tests for src/calendar_replay.py parser pieces.

The parser converts Tock's protobuf calendar/full body bytes into
AvailableSlot objects. This is the safety-critical piece — a parser
bug could cause the bot to think slots exist when they don't (would
race against ghost slots and fail) or vice versa (would miss real
release moments).

The HTTP/Playwright pieces require live browser context and are
exercised by the spike scripts; here we focus on the byte-level
parser correctness.
"""
from datetime import date

import pytest


def _bookable_section(date_iso: str, times: list[str]) -> bytes:
    """Build a date section that passes the ghost filter (≥800 bytes,
    ≥5 times). Used by tests that focus on parser correctness rather
    than the ghost-filter behavior (which has its own dedicated tests
    in test_replay_ghost_filter.py)."""
    body = b"\x0a\x0a" + date_iso.encode()
    # Pack the requested times with realistic framing
    for t in times:
        body += b"\x12\\\x1a\x05" + t.encode() + b"\x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
    # Pad with extra distinct times if fewer than 5 to pass the time-count threshold
    pad_times = ["10:30", "11:30", "12:30", "13:30", "14:30"]
    for t in pad_times:
        if len([x for x in body if False]) >= 0:  # always pad
            body += b"\x12\\\x1a\x05" + t.encode() + b"\x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
    # Pad to >= 800 bytes
    if len(body) < 900:
        body += b"\x00" * (900 - len(body))
    return body


def test_parse_finds_dates_with_times():
    """A body with one date + 3 booking-hour times → 3 slots
    (after we ignore the morning padding times added to satisfy the
    ghost-filter threshold)."""
    from src.calendar_replay import parse_available_slots
    body = _bookable_section("2026-05-15", ["17:30", "18:00", "20:30"])
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    times = {s.slot_time for s in slots}
    assert {"5:30 PM", "6:00 PM", "8:30 PM"}.issubset(times)
    assert all(s.slot_date == date(2026, 5, 15) for s in slots)
    assert all(s.day_of_week == "Friday" for s in slots)


def test_parse_filters_to_target_dates():
    """A body containing dates A and B; if only A is in target_dates,
    B's slots are dropped."""
    from src.calendar_replay import parse_available_slots
    body = _bookable_section("2026-05-15", ["17:30"]) + \
           _bookable_section("2026-05-22", ["18:00"])
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert all(s.slot_date == date(2026, 5, 15) for s in slots)
    assert any(s.slot_time == "5:30 PM" for s in slots)


def test_parse_filters_out_implausible_times():
    """Protobuf framing bytes can look like 00:00, 02:30, etc.
    Only bookable hours (10:00–23:59) should produce slots."""
    from src.calendar_replay import parse_available_slots
    # Build a section that contains both implausible times AND real ones
    body = _bookable_section("2026-05-15", ["17:30", "21:00"])
    # Inject some implausible times into the section (won't count toward the 5-time threshold for filter, but parser should drop them from output)
    body = (
        b"\x0a\x0a2026-05-15"
        + b"\x1a\x0500:00\x1a\x0502:30\x1a\x0508:00"  # all should be dropped
        + body[len(b"\x0a\x0a2026-05-15"):]
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    times = {s.slot_time for s in slots}
    # Real bookable times kept
    assert "5:30 PM" in times
    assert "9:00 PM" in times
    # Implausible times never appear
    assert not any(t.startswith("12:") and "AM" in t for t in times)
    assert not any(t.startswith("2:") and "AM" in t for t in times)
    assert not any(t.startswith("8:") and "AM" in t for t in times)


def test_parse_deduplicates_repeated_times():
    """Tock's protobuf repeats date strings (and sometimes times).
    Each (date, time) pair should appear exactly once in output."""
    from src.calendar_replay import parse_available_slots
    # Build a real-shaped body with 17:30 repeated 3× and 20:00 once
    base = _bookable_section("2026-05-15", ["17:30", "17:30", "17:30", "20:00"])
    # Also repeat the date marker (as Tock does)
    body = b"\x0a\x0a2026-05-15" + base
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    times = {s.slot_time for s in slots}
    assert "5:30 PM" in times
    assert "8:00 PM" in times
    # Each (date, time) appears exactly once (set-deduplication)
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
    body = b"\x0a\x0a2026-05-22\x1a\x0517:30\x00"
    assert parse_available_slots(body, [date(2026, 5, 15)]) == []


def test_parse_orders_slots_by_date_then_time():
    """Output is sorted: dates ascending, times ascending within each date."""
    from src.calendar_replay import parse_available_slots
    body = (
        _bookable_section("2026-05-22", ["20:00", "17:30"])  # times out of order
        + _bookable_section("2026-05-15", ["18:00", "17:30"])
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
    # Required times present per date
    assert "5:30 PM" in times_by_date["2026-05-15"]
    assert "6:00 PM" in times_by_date["2026-05-15"]
    assert "5:30 PM" in times_by_date["2026-05-22"]
    assert "8:00 PM" in times_by_date["2026-05-22"]


def test_parse_handles_real_benu_body_shape():
    """Smoke test against the real shape captured from benu's calendar/full
    response (per spikes/http_replay/benu_trace.json sample). Each date
    section gets the date marker repeated 2× plus time entries plus
    bulk filler to pass the ghost filter (≥800b, ≥5 times per section)."""
    from src.calendar_replay import parse_available_slots
    # Build benu-like sections: date repeated 2×, 5+ times, padded
    section_a = (
        b"\x0a\x0a2026-05-13\x0a\x0a2026-05-13" +
        _bookable_section("2026-05-13", ["17:30", "18:00", "18:30", "19:30"])
    )
    section_b = (
        b"\x0a\x0a2026-05-24\x0a\x0a2026-05-24" +
        _bookable_section("2026-05-24", ["17:30", "20:30"])
    )
    body = section_a + section_b
    slots = parse_available_slots(
        body, [date(2026, 5, 13), date(2026, 5, 24)]
    )
    times_by_date = {}
    for s in slots:
        times_by_date.setdefault(s.slot_date.isoformat(), set()).add(s.slot_time)
    # Real benu times for each date are present (extras from padding times allowed)
    assert {"5:30 PM", "6:00 PM", "6:30 PM", "7:30 PM"}.issubset(
        times_by_date.get("2026-05-13", set())
    )
    assert {"5:30 PM", "8:30 PM"}.issubset(
        times_by_date.get("2026-05-24", set())
    )


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
