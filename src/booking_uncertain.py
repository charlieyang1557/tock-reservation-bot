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
import time
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path

logger = logging.getLogger(__name__)

UNCERTAIN_FILE = Path("booking_uncertain.json")

# B2.1: how many days a booking_uncertain.json may sit before it's
# treated as stale. The booker only needs the guard until the operator
# verifies on Tock — beyond a week, the bot has been blocked too long
# and the booking decision is no longer relevant. Auto-archive it so
# future races aren't blocked indefinitely.
STALE_AFTER_DAYS = 7


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
    exists / is unreadable / is stale.

    B2.1: a slot_date_str more than STALE_AFTER_DAYS in the past is
    treated as stale — the file is archived to
    `<path>.archive/<timestamp>__<basename>` and None is returned so
    future races aren't blocked indefinitely by an operator forgetting
    to clear the file. A malformed slot_date_str is also treated as
    stale and archived (defensive: a permanently-malformed file would
    otherwise never expire).
    """
    if not path.exists():
        return None

    try:
        data = json.loads(path.read_text())
        booking = UncertainBooking(**data)
    except Exception as e:
        logger.error(
            f"[uncertain-booking] {path} exists but is unreadable: {e}. "
            "Archiving and treating as no uncertain booking — operator "
            "should investigate the archive."
        )
        _archive_uncertain(path, reason="unreadable")
        return None

    # Stale-by-date check
    try:
        slot_date = date.fromisoformat(booking.slot_date_str)
    except (ValueError, TypeError) as e:
        logger.warning(
            f"[uncertain-booking] {path} has invalid slot_date_str "
            f"{booking.slot_date_str!r}: {e}. Archiving as stale."
        )
        _archive_uncertain(path, reason="bad-date")
        return None

    age_days = (date.today() - slot_date).days
    if age_days > STALE_AFTER_DAYS:
        logger.warning(
            f"[uncertain-booking] {path} is stale "
            f"(slot_date={booking.slot_date_str}, age={age_days}d > "
            f"{STALE_AFTER_DAYS}d threshold). Archiving so future races "
            "are not blocked indefinitely. Audit trail in "
            f"{path.parent / (path.stem + '.archive')}/."
        )
        _archive_uncertain(path, reason="stale")
        # Codex B2 fix: if archive failed (file still on disk), keep
        # blocking races by returning the booking object. The next
        # cycle will retry the archive. Better to block races for
        # longer than to silently unblock.
        if path.exists():
            logger.warning(
                f"[uncertain-booking] Archive failed; {path} still "
                "exists. Continuing to block races until disk state "
                "is repaired."
            )
            return booking
        return None

    return booking


def _archive_uncertain(path: Path, *, reason: str) -> None:
    """Move `path` to `<path>.archive/<timestamp>_<reason>__<basename>`.

    Codex B2 review HIGH: failures are logged loudly but the live file
    is LEFT IN PLACE. Removing it on archive failure would silently
    drop the operator's safety guard and let the bot race on a slot
    whose outcome was never verified — strictly worse than continuing
    to block races until the operator fixes the underlying disk issue.
    """
    try:
        archive_dir = path.parent / (path.stem + ".archive")
        archive_dir.mkdir(parents=True, exist_ok=True)
        # Use a high-resolution timestamp so back-to-back archives don't
        # collide if the same path is archived twice in <1 second.
        ts = time.strftime("%Y%m%dT%H%M%S") + f"_{int(time.time() * 1e6) % 1_000_000:06d}"
        dest = archive_dir / f"{ts}_{reason}__{path.name}"
        path.replace(dest)
        logger.info(f"[uncertain-booking] Archived {path} → {dest}")
    except Exception as e:
        logger.error(
            f"[uncertain-booking] Archive of {path} failed: "
            f"{type(e).__name__}: {e}. LEAVING the live file in place "
            "so the no-race guard remains active. Operator must "
            f"investigate disk state and clear {path} manually after "
            "verifying the booking on Tock."
        )


def clear_uncertain(path: Path = UNCERTAIN_FILE) -> None:
    """Remove the persisted uncertain-booking file. Used by tests and
    by the documented operator clear-and-restart procedure."""
    try:
        path.unlink(missing_ok=True)
    except Exception as e:
        logger.error(f"[uncertain-booking] Failed to remove {path}: {e}")
