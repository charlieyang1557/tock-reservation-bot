"""Persistent guard for unverifiable booking confirmations.

When booker._book_single clicks confirm but cannot verify success, the
slot may have been accepted by Tock. We MUST NOT attempt another booking
in the same process OR after restart until the operator manually verifies
on Tock and clears the state.

State is stored as a small JSON file at the project root so:
  - It survives main.py's auto-restart loop (which constructs a fresh
    TockBooker, resetting in-memory state).
  - It survives Mac mini reboots / PM2 restarts.
  - The operator can clear it explicitly with `rm booking_uncertain.json`
    after verifying on https://www.exploretock.com/account/reservations.

Codex pass 3 caught the original in-memory-only design as a HIGH bug.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

UNCERTAIN_FILE = Path("booking_uncertain.json")


@dataclass(frozen=True)
class UncertainBooking:
    """Snapshot of a booking attempt whose outcome could not be verified."""
    slot_date_str: str    # ISO date like "2026-05-01"
    slot_time: str        # "5:00 PM"
    day_of_week: str      # "Friday"
    detected_at_iso: str  # ISO timestamp when the soft-win fired


def write_uncertain(booking: UncertainBooking, path: Path = UNCERTAIN_FILE) -> None:
    """Write the uncertain-booking snapshot to disk. Best-effort: I/O errors
    are logged but do NOT raise — failing to write the file is bad, but
    raising here would mask the original soft-win path."""
    try:
        path.write_text(json.dumps(asdict(booking), indent=2))
        logger.warning(
            f"[uncertain-booking] State written to {path.resolve()} — "
            "bot will refuse all future booking attempts until this file "
            "is removed by the operator."
        )
    except Exception as e:
        logger.error(
            f"[uncertain-booking] Failed to write {path}: {e}. "
            "In-memory guard will still apply for THIS process — but the "
            "guard is lost on restart."
        )


def read_uncertain(path: Path = UNCERTAIN_FILE) -> UncertainBooking | None:
    """Return the persisted uncertain-booking snapshot, or None if no file
    exists / file is unreadable. Unreadable file is treated as 'no
    uncertain booking' to avoid bricking the bot on disk corruption — but
    the corruption is logged loudly."""
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return UncertainBooking(**data)
    except Exception as e:
        logger.error(
            f"[uncertain-booking] {path} exists but is unreadable: {e}. "
            "Treating as no uncertain booking — but operator should "
            "investigate."
        )
        return None


def clear_uncertain(path: Path = UNCERTAIN_FILE) -> None:
    """Remove the persisted uncertain-booking file. Used by tests and
    by the documented operator clear-and-restart procedure."""
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"[uncertain-booking] Failed to remove {path}: {e}")
