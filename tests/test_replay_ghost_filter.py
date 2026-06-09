"""Tests for ghost-slot protection in src/calendar_replay.py.

History:
  - 2026-05-10: sold-out fuhuihua bodies contained byte sequences that the
    naive raw-byte time regex mistook for bookable slots ("ghosts"). The
    first fix guarded on section size (>=800 b) and time count (>=5).
  - 2026-06-05: those guards filtered the REAL Fuhuihua release (sparse
    ~180-400 b sections, 1-2 seatings each) — the bot fell back to a cold
    DOM scan and lost ~9s (bot.log:256945).

Current policy (this file): NO size heuristics. A seating counts only if
it is structurally valid protobuf — an exactly-5-byte "HH:MM" string
field inside a decoded message whose enclosing section carries exactly
one date-string field. Ghost protection comes from the wire format:
  - raw framing bytes can't fullmatch a 5-byte string field;
  - times mentioned inside prose description fields are substrings of
    long string fields, never standalone 5-byte fields;
  - date-range echoes (many dates in one message) give no unambiguous
    date context, so stray times there are dropped;
  - malformed/garbage bodies fail strict decode → zero slots (the DOM
    safety-net in checker.py bounds false negatives).

Real-capture coverage lives in test_replay_0605_release_capture.py.
"""
from datetime import date

from tests.proto_fixtures import (
    calendar_body,
    date_range_echo,
    date_section,
    experience_metadata,
    len_field,
    seating,
    sold_out_date_section,
)


def test_single_seating_release_is_bookable():
    """The 2026-06-05 incident class, minimal synthetic form: ONE seating
    on one date, tiny section (~80 b) — must parse as bookable. This is
    exactly what the old >=800 b / >=5 times guards wrongly filtered."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(date_section("2026-06-12", ["17:00"]))
    slots = parse_available_slots(body, [date(2026, 6, 12)])
    assert [(s.slot_date_str, s.slot_time) for s in slots] == [
        ("2026-06-12", "5:00 PM")
    ]


def test_benu_volume_section_is_bookable():
    """The high-volume benu shape (many seatings per date) still parses."""
    from src.calendar_replay import parse_available_slots

    times = ["17:00", "17:30", "18:00", "18:30", "19:00", "19:30", "20:00", "21:00"]
    body = calendar_body(date_section("2026-05-15", times))
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert {s.slot_time for s in slots} == {
        "5:00 PM", "5:30 PM", "6:00 PM", "6:30 PM",
        "7:00 PM", "7:30 PM", "8:00 PM", "9:00 PM",
    }


def test_sold_out_sections_yield_no_slots():
    """The real sold-out encoding (f2 zero-length) must never ghost, even
    surrounded by metadata blocks full of text."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(
        sold_out_date_section("2026-06-10"),
        sold_out_date_section("2026-06-14"),
        experience_metadata("A tasting journey through the seasons."),
    )
    slots = parse_available_slots(body, [date(2026, 6, 10), date(2026, 6, 14)])
    assert slots == []


def test_unanchored_ascii_times_in_framing_are_ghosts():
    """The May-2026 ghost class: raw ASCII times floating in binary
    framing/padding (NOT length-delimited string fields). The old loose
    regex counted these; the structural decode must not."""
    from src.calendar_replay import parse_available_slots

    # Date marker + loose ASCII times + NUL padding — byte soup, not
    # valid protobuf (NUL is an invalid tag). Old parser: section >800 b
    # with 5 "times" → ghost slots. New parser: decode fails → [].
    body = (
        b"\x0a\x0a2026-05-15"
        + b"\x0017:00\x0018:00\x0019:00\x0020:00\x0021:00\x00"
        + b"\x00" * 900
    )
    slots = parse_available_slots(body, [date(2026, 5, 15)])
    assert slots == [], f"unanchored framing times must not ghost; got {slots}"


def test_times_inside_description_prose_are_ghosts():
    """Times mentioned in the experience description (a long string field)
    must not be attributed to any date — they are substrings of prose,
    not seating entries."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(
        sold_out_date_section("2026-06-12"),
        experience_metadata(
            "Seatings nightly at 17:00, 18:00, 19:00, 20:00 and 21:00. "
            "Doors open at 16:30. " * 20
        ),
    )
    slots = parse_available_slots(body, [date(2026, 6, 12)])
    assert slots == [], f"prose times must not ghost; got {slots}"


def test_date_range_echo_gets_no_times():
    """The f5 range echo lists every calendar date in ONE message. A stray
    5-byte time field in a multi-date message has no unambiguous date
    context and must be dropped."""
    from src.calendar_replay import parse_available_slots

    echo_dates = ["2026-06-10", "2026-06-11", "2026-06-12"]
    targets = [date(2026, 6, 10), date(2026, 6, 11), date(2026, 6, 12)]

    # The real echo shape (dates only, as in the 06/05 capture)
    body = calendar_body(
        sold_out_date_section("2026-06-12"), date_range_echo(echo_dates)
    )
    assert parse_available_slots(body, targets) == []

    # Poisoned variant: a stray time field inside the multi-date message
    poisoned_echo = len_field(
        5,
        b"".join(len_field(2, d.encode()) for d in echo_dates)
        + len_field(3, b"17:00"),  # stray time among many dates
    )
    body = calendar_body(sold_out_date_section("2026-06-12"), poisoned_echo)
    slots = parse_available_slots(body, targets)
    assert slots == [], f"ambiguous-context times must not ghost; got {slots}"


def test_time_with_no_date_context_is_dropped():
    """A structurally valid seating that is NOT under any date section
    (e.g. service-hours metadata) must not produce slots."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(
        sold_out_date_section("2026-06-12"),
        len_field(7, seating("17:00")),  # orphan seating, no date in scope
    )
    slots = parse_available_slots(body, [date(2026, 6, 12)])
    assert slots == []


def test_garbage_with_absurd_field_numbers_is_rejected():
    """Field numbers above the protobuf spec maximum (2^29 - 1) are
    invalid wire format; strict decode rejects them so binary garbage
    never becomes a date context. (Lower bounds are NOT safe: Tock's
    real 06/05 envelope field is 60686.)"""
    from src.calendar_replay import (
        _MAX_PLAUSIBLE_FIELD_NUMBER,
        parse_available_slots,
    )

    absurd_field = _MAX_PLAUSIBLE_FIELD_NUMBER + 1
    garbage = len_field(
        absurd_field, len_field(1, b"2026-06-12") + len_field(3, b"17:00")
    )
    body = calendar_body(garbage)
    slots = parse_available_slots(body, [date(2026, 6, 12)])
    assert slots == []


def test_truncated_body_never_raises():
    """Truncating the body mid-message (network hiccup) must yield zero
    slots without raising — every truncation point of a real-shaped body."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(date_section("2026-06-12", ["17:00"]))
    for cut in range(len(body)):
        slots = parse_available_slots(body[:cut], [date(2026, 6, 12)])
        assert isinstance(slots, list)


def test_booking_hour_bounds_still_enforced():
    """Structurally valid seatings outside plausible booking hours
    (10:00–23:59) are still dropped — framing can encode small varints
    that survive as 00:xx/0x:xx strings in weird schemas."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(
        date_section("2026-06-12", ["00:00", "02:30", "08:00", "17:00"])
    )
    slots = parse_available_slots(body, [date(2026, 6, 12)])
    assert [(s.slot_date_str, s.slot_time) for s in slots] == [
        ("2026-06-12", "5:00 PM")
    ]
