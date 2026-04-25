"""
Singleton process lock for Tock bot.

Uses fcntl.flock() on a lock file to ensure only one bot instance runs at
a time. Stale locks (whose owning PID is no longer alive) are reclaimed
automatically — no manual cleanup needed after a hard kill.

Usage:
    from src.process_lock import acquire_singleton_lock
    lock_handle = acquire_singleton_lock("bot.lock")
    # ... run bot ...
    # Lock auto-released when process exits (or call lock_handle.close())
"""

import fcntl
import logging
import os
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


class LockAcquisitionError(SystemExit):
    """Raised (as SystemExit) when the lock cannot be acquired."""


def _pid_alive(pid: int) -> bool:
    """Return True iff the given PID is currently running."""
    try:
        os.kill(pid, 0)  # signal 0 = no-op, raises if process gone
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except OSError:
        return False


def _read_holder_pid(lock_path: str) -> int | None:
    """Read the PID written by the previous holder, or None if unreadable."""
    try:
        text = Path(lock_path).read_text().strip()
        return int(text) if text else None
    except (FileNotFoundError, ValueError, OSError):
        return None


def acquire_singleton_lock(lock_path: str = "bot.lock"):
    """
    Acquire an exclusive flock on `lock_path`. Exit non-zero if another live
    process holds the lock. Returns the file handle (caller must keep it
    alive — closing the handle releases the lock).
    """
    fh = open(lock_path, "a+")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        holder = _read_holder_pid(lock_path)
        if holder is not None and _pid_alive(holder):
            msg = (
                f"Another bot instance is already running "
                f"(PID {holder} holds lock on {lock_path}).\n"
                f"  Stop the other instance first, or kill it with: kill {holder}"
            )
            logger.error(msg)
            print(msg, file=sys.stderr)
            fh.close()
            raise LockAcquisitionError(2)
        # Stale lock — try once more after truncating
        logger.warning(
            f"Stale lock at {lock_path} (PID {holder} not alive) — reclaiming."
        )
        fh.seek(0)
        fh.truncate()
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            msg = f"Could not reclaim stale lock at {lock_path}."
            logger.error(msg)
            print(msg, file=sys.stderr)
            fh.close()
            raise LockAcquisitionError(2)

    # Write our PID into the lock file so the next reader can see it
    fh.seek(0)
    fh.truncate()
    fh.write(f"{os.getpid()}\n")
    fh.flush()
    logger.info(f"[startup] Acquired {lock_path} (PID={os.getpid()})")
    return fh
