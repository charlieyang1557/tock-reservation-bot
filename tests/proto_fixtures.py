"""Builders for structurally VALID Tock calendar/full protobuf bodies.

Modeled byte-for-byte on two real captures:
  - replay_captures/20260605T200009_780186_fui-hui-hua-san-francisco_replay.bin
    (the real 2026-06-05 20:00 PT Fuhuihua release — sparse, 1-2 seatings/date)
  - spikes/http_replay/benu_trace.json calendar/full entry
    (high-volume benu shape — 11-17 seatings/date)

Both shapes share the same wire structure:

  body = f1 { f1 {                       # two wrapper levels
      f1 { f1: "YYYY-MM-DD"              # date section, repeated
           f2 { ... f2 { f3: "HH:MM"     # seating entry (slot)
                         <metadata> } } }
      ...
      f2 { <experience metadata, long text strings> }
      f5 { f2: "YYYY-MM-DD" * N }        # requested date-range echo
  } }

  A SOLD-OUT date section is just  f1 { f1: "YYYY-MM-DD"  f2: <empty> }.

The old parser regex-scanned raw bytes and guarded with size heuristics;
these builders exist so tests exercise the structural decode against
honest wire-format bodies instead of hand-glued byte soup.
"""


def varint(n: int) -> bytes:
    if n < 0:
        raise ValueError(f"varint requires n >= 0, got {n}")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def len_field(field_no: int, payload: bytes) -> bytes:
    """A length-delimited field (wire type 2)."""
    return varint((field_no << 3) | 2) + varint(len(payload)) + payload


def varint_field(field_no: int, value: int) -> bytes:
    """A varint field (wire type 0)."""
    return varint(field_no << 3) + varint(value)


def seating(time_str: str, remaining: int = 8) -> bytes:
    """One seating entry: f2 { f3: "HH:MM", capacity varints, metadata } —
    the shape every real seating has in the fuhuihua captures. f4=total
    seats, f5=REMAINING (the availability discriminator — the 2026-06-10
    live-ghost incident proved sold-out seatings persist in the body with
    f5=0), f7=booked."""
    nested_availability = len_field(1, varint_field(1, 3) + varint_field(2, 1))
    inner = (
        len_field(3, time_str.encode())
        + varint_field(4, 8)
        + varint_field(5, remaining)
        + varint_field(6, 0)
        + varint_field(7, 8 - remaining)
        + varint_field(9, 1)
        + len_field(13, nested_availability)
    )
    return len_field(2, inner)


def sold_out_seating(time_str: str) -> bytes:
    """A seating that already sold (f5=0, f7=8) — present in the wire body
    but NOT bookable. The exact shape of 06-06/06-07 in the 06/05 capture
    and of 06-11/06-12/06-13 in the 06/10 sold-out capture."""
    return seating(time_str, remaining=0)


def date_section(date_iso: str, times: list[str]) -> bytes:
    """A bookable date section in the real fuhuihua release shape:
    f1 { f1: date, f2 { f1 { f1: date, f2 { seatings... } } } }."""
    seatings = b"".join(seating(t) for t in times)
    inner_day = len_field(1, len_field(1, date_iso.encode()) + len_field(2, seatings))
    return len_field(1, len_field(1, date_iso.encode()) + len_field(2, inner_day))


def sold_out_date_section(date_iso: str) -> bytes:
    """An empty (sold out / not released) date section, exactly as the
    real capture encodes it: f1 { f1: date, f2: <0 bytes> }."""
    return len_field(1, len_field(1, date_iso.encode()) + len_field(2, b""))


def experience_metadata(description: str) -> bytes:
    """The f2 experience-metadata block — long human-text string fields.
    The real capture's description is 1441 bytes of prose; ghost
    protection requires that times mentioned in it never become slots."""
    return len_field(
        2,
        len_field(2, b"Tasting Menu experience")
        + len_field(3, description.encode())
        + len_field(106, b"FHH"),
    )


def date_range_echo(date_isos: list[str]) -> bytes:
    """The f5 requested-range echo: every calendar date as a bare string,
    bookable or not. A naive parser that attributes nearby times to these
    markers produces ghosts."""
    return len_field(5, b"".join(len_field(2, d.encode()) for d in date_isos))


def calendar_body(*parts: bytes) -> bytes:
    """Wrap sections/metadata in the two f1 envelope levels of the real
    response."""
    return len_field(1, len_field(1, b"".join(parts)))
