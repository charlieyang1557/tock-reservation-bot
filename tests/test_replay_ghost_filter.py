"""Tests for the ghost-slot filter in src/calendar_replay.py.

Discovery 2026-05-10: when a Tock restaurant is fully sold out
(verified against fuhuihua), the calendar/full protobuf body still
contains time-string metadata that looks like bookable slots to a
naive byte-scan parser. The filter rejects:
  - date sections below _MIN_BOOKABLE_SECTION_BYTES (real slot
    metadata is bulky; sold-out sections are ~16-322 bytes vs
    ~1000-1500 for available dates)
  - sections with fewer than _MIN_BOOKABLE_TIMES_PER_SECTION time
    matches (a real bookable date offers 5+ anchor times; ghost
    sections show 1-2 framing artifacts)
"""
from datetime import date

import pytest


def test_filter_keeps_real_benu_shape_section():
    """A 1500-byte section with 8 time strings (benu shape) is kept."""
    from src.calendar_replay import parse_available_slots

    # Build a section: date marker + many time markers + bulk filler
    # Total per date section must be >= 800 bytes
    times_block = b"".join(
        f"\x12\\\x1a\x05{h:02d}:{m:02d} ".encode() + b"\x04(\x000\x008\x04@\x00H\x03X\x00`\x00j("
        for h, m in [(17, 30), (18, 0), (18, 30), (19, 0), (19, 30), (20, 0), (20, 30), (21, 0)]
    )
    bulk_filler = b"\x00" * (1000 - len(times_block))
    body = b"\x0a\x0a2026-05-15" + times_block + bulk_filler + b"\x0a\x0a2026-05-22\x00"

    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert len(slots) >= 5, (
        f"benu-shape section (>800b, 8 times) must yield slots; got {len(slots)}"
    )


def test_filter_drops_fuhuihua_sold_out_section():
    """A small section (sold out) with 1-2 framing artifact times is dropped."""
    from src.calendar_replay import parse_available_slots

    # Sold-out fuhuihua shape: tiny section + 1 framing-artifact time
    # Total section must be < 800 bytes AND time-count < 5
    body = (
        b"\x0a\x0a2026-05-15"
        b"\x12\x05\x1a\x0517:00\x00"  # one ghost time at 17:00
        + b"\x00" * 100  # padding to reach realistic small section
        + b"\x0a\x0a2026-05-22\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert slots == [], (
        f"sold-out section (small, 1 ghost time) must yield 0 slots; got {slots}"
    )


def test_filter_drops_section_with_few_time_matches():
    """A section that meets size but has < 5 times is still dropped
    (insufficient slot data → likely framing artifacts)."""
    from src.calendar_replay import parse_available_slots

    # Section meets _MIN_BOOKABLE_SECTION_BYTES but only 2 times
    body = (
        b"\x0a\x0a2026-05-15"
        + b"\x00" * 800
        + b"\x12\x05\x1a\x0517:00\x12\x05\x1a\x0518:00"
        + b"\x0a\x0a2026-05-22\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert slots == [], (
        f"section with <5 times must be dropped (ghost-prone); got {slots}"
    )


def test_filter_keeps_section_meeting_both_thresholds():
    """A section meeting BOTH thresholds is kept (both size AND 5+ times)."""
    from src.calendar_replay import parse_available_slots

    # 800+ bytes AND 6 distinct booking-hour times
    body = (
        b"\x0a\x0a2026-05-15"
        + b"\x12\x05\x1a\x0517:30"
        + b"\x12\x05\x1a\x0518:00"
        + b"\x12\x05\x1a\x0518:30"
        + b"\x12\x05\x1a\x0519:00"
        + b"\x12\x05\x1a\x0519:30"
        + b"\x12\x05\x1a\x0520:00"
        + b"\x00" * 1000  # bulk filler to push section past 800b
        + b"\x0a\x0a2026-05-22\x00"
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert len(slots) >= 5, (
        f"section meeting both thresholds must yield slots; got {len(slots)}"
    )


def test_filter_thresholds_are_module_constants():
    """The thresholds must be exposed for ops to tune if Tock's protobuf
    shape changes."""
    from src.calendar_replay import (
        _MIN_BOOKABLE_SECTION_BYTES,
        _MIN_BOOKABLE_TIMES_PER_SECTION,
    )
    assert _MIN_BOOKABLE_SECTION_BYTES >= 100  # sanity: not too low
    assert _MIN_BOOKABLE_TIMES_PER_SECTION >= 1
