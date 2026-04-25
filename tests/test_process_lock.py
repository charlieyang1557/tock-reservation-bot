"""Tests for the singleton process lock — second process must refuse to start."""
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _spawn(lock_path: str, hold_secs: float) -> subprocess.Popen:
    """Spawn a subprocess that acquires the lock and holds it for hold_secs."""
    code = (
        "import sys, time\n"
        "sys.path.insert(0, '.')\n"
        "from src.process_lock import acquire_singleton_lock\n"
        f"lock = acquire_singleton_lock('{lock_path}')\n"
        f"time.sleep({hold_secs})\n"
    )
    return subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_second_acquire_fails():
    """A second process trying to acquire the same lock must exit non-zero."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        first = _spawn(lock_path, hold_secs=2.0)
        time.sleep(0.5)  # give first process time to acquire

        # Second process attempts the same lock
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0, "Second process must fail to acquire"
        assert "lock" in (result.stderr + result.stdout).lower()

        first.wait(timeout=5)


def test_lock_released_on_process_exit():
    """After the holder exits, a fresh process must acquire the lock cleanly."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        first = _spawn(lock_path, hold_secs=0.5)
        first.wait(timeout=5)

        # Second process should now succeed
        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0


def test_stale_lock_reclaimed():
    """A lock file whose owning PID is dead must be reclaimable."""
    with tempfile.TemporaryDirectory() as tmp:
        lock_path = os.path.join(tmp, "test.lock")
        # Write a fake PID that definitely isn't running
        Path(lock_path).write_text("999999\n")

        result = subprocess.run(
            [sys.executable, "-c",
             f"import sys; sys.path.insert(0, '.');\n"
             f"from src.process_lock import acquire_singleton_lock;\n"
             f"acquire_singleton_lock('{lock_path}')\n"],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode == 0, (
            f"Stale lock should be reclaimable; got stderr={result.stderr}"
        )
