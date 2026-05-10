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


def test_parse_finds_dates_with_times():
    """A simple body with one date + 3 times → 3 slots."""
    from src.calendar_replay import parse_available_slots
    # Mimic protobuf framing: garbage bytes + date + framing + times
    body = (
        b"\x00\x12\x0a\x0a2026-05-15\x12\x0a"
        b"\x1a\x0517:30\x00\x00"
        b"\x1a\x0518:00\x00\x00"
        b"\x1a\x0520:30\x00\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert len(slots) == 3
    assert {s.slot_time for s in slots} == {"5:30 PM", "6:00 PM", "8:30 PM"}
    assert all(s.slot_date == date(2026, 5, 15) for s in slots)
    assert all(s.day_of_week == "Friday" for s in slots)


def test_parse_filters_to_target_dates():
    """A body containing dates A and B; if only A is in target_dates,
    B's slots are dropped."""
    from src.calendar_replay import parse_available_slots
    body = (
        b"\x0a\x0a2026-05-15\x12\x05"
        b"\x1a\x0517:30\x00\x00"
        b"\x0a\x0a2026-05-22\x12\x05"
        b"\x1a\x0518:00\x00\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert len(slots) == 1
    assert slots[0].slot_date == date(2026, 5, 15)
    assert slots[0].slot_time == "5:30 PM"


def test_parse_filters_out_implausible_times():
    """Protobuf framing bytes can look like 00:00, 02:30, etc.
    Only bookable hours (10:00–23:59) should produce slots."""
    from src.calendar_replay import parse_available_slots
    body = (
        b"\x0a\x0a2026-05-15\x12\x05"
        b"\x1a\x0500:00"   # framing — drop
        b"\x1a\x0502:30"   # framing — drop
        b"\x1a\x0508:00"   # too early — drop
        b"\x1a\x0517:30"   # real slot — keep
        b"\x1a\x0521:00"   # real slot — keep
        b"\x00\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert {s.slot_time for s in slots} == {"5:30 PM", "9:00 PM"}


def test_parse_deduplicates_repeated_times():
    """Tock's protobuf repeats date strings (and sometimes times).
    Each (date, time) pair should appear exactly once in output."""
    from src.calendar_replay import parse_available_slots
    body = (
        b"\x0a\x0a2026-05-15\x0a\x0a2026-05-15"  # date repeated
        b"\x1a\x0517:30\x1a\x0517:30\x1a\x0517:30"  # time repeated
        b"\x1a\x0520:00\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert len(slots) == 2  # 17:30 once + 20:00 once
    assert {s.slot_time for s in slots} == {"5:30 PM", "8:00 PM"}


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
        b"\x0a\x0a2026-05-22"
        b"\x1a\x0520:00\x1a\x0517:30"  # times out of order
        b"\x0a\x0a2026-05-15"
        b"\x1a\x0518:00\x1a\x0517:30"
        b"\x00"
    )
    slots = parse_available_slots(
        body, [date(2026, 5, 15), date(2026, 5, 22)]
    )
    # Dates sorted ascending, times sorted ascending within each date
    assert [(s.slot_date.isoformat(), s.slot_time) for s in slots] == [
        ("2026-05-15", "5:30 PM"),
        ("2026-05-15", "6:00 PM"),
        ("2026-05-22", "5:30 PM"),
        ("2026-05-22", "8:00 PM"),
    ]


def test_parse_handles_real_benu_body_shape():
    """Smoke test against the real shape captured from benu's calendar/full
    response (per spikes/http_replay/benu_trace.json sample)."""
    from src.calendar_replay import parse_available_slots
    # Approximate the real body layout: each date appears 2x consecutively,
    # then ~4 unique times follow per date.
    body = (
        b"\x0a\x0a2026-05-13\x0a\x0a2026-05-13"
        b"\x12\\\x1a\x0517:30 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        b"\x12\\\x1a\x0518:00 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        b"\x12\\\x1a\x0518:30 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        b"\x12\\\x1a\x0519:30 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        b"\x0a\x0a2026-05-24\x0a\x0a2026-05-24"
        b"\x12\\\x1a\x0517:30 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        b"\x12\\\x1a\x0520:30 \x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
    )
    slots = parse_available_slots(
        body, [date(2026, 5, 13), date(2026, 5, 24)]
    )
    times_by_date = {}
    for s in slots:
        times_by_date.setdefault(s.slot_date.isoformat(), set()).add(s.slot_time)
    assert times_by_date == {
        "2026-05-13": {"5:30 PM", "6:00 PM", "6:30 PM", "7:30 PM"},
        "2026-05-24": {"5:30 PM", "8:30 PM"},
    }


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
