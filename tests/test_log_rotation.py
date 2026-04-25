"""Tests for bot.log rotation (Phase A+1).

Production bot.log was reported at 641 MB after ~12 days of dual-process
logging. With the singleton lock from Phase A this is roughly halved, but
unbounded growth is still a problem. This test pins the rotation policy
so disk usage stays bounded and recent history stays accessible.
"""
import logging
import os
import tempfile
from logging.handlers import RotatingFileHandler

import pytest

import main as main_mod


@pytest.fixture(autouse=True)
def _reset_logging():
    """Snapshot and restore root logger state around each test.

    main._setup_logging() mutates the root logger; without restoration
    later tests would inherit our handlers and config and behave
    unpredictably.
    """
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    yield
    for h in root.handlers[:]:
        try:
            h.close()
        except Exception:
            pass
        root.removeHandler(h)
    for h in saved_handlers:
        root.addHandler(h)
    root.setLevel(saved_level)


def test_setup_logging_uses_rotating_file_handler():
    """The file handler must be a RotatingFileHandler (not plain FileHandler)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bot.log")
        main_mod._setup_logging(log_path=path)

        file_handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.FileHandler)
        ]
        assert len(file_handlers) == 1, (
            f"Expected exactly one file handler; got {len(file_handlers)}"
        )
        assert isinstance(file_handlers[0], RotatingFileHandler), (
            f"File handler must be RotatingFileHandler; got {type(file_handlers[0]).__name__}"
        )


def test_rotation_policy_is_bounded():
    """Default rotation policy must cap total log usage well below the
    641 MB observed in production. The exact numbers are tunable but
    the invariant is: max_bytes × (backup_count + 1) ≤ 250 MB.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bot.log")
        main_mod._setup_logging(log_path=path)

        rfh = next(
            h for h in logging.getLogger().handlers
            if isinstance(h, RotatingFileHandler)
        )
        ceiling = rfh.maxBytes * (rfh.backupCount + 1)
        assert ceiling <= 250 * 1024 * 1024, (
            f"Rotation ceiling {ceiling:,} bytes exceeds 250 MB budget"
        )
        assert rfh.maxBytes >= 5 * 1024 * 1024, (
            f"max_bytes {rfh.maxBytes:,} too small — would rotate during a single sniper window"
        )
        assert rfh.backupCount >= 3, (
            f"backup_count={rfh.backupCount} keeps too little history for debugging"
        )


def test_rotation_creates_numbered_backups():
    """Writing past max_bytes triggers rotation: bot.log.1 (and .2 if needed) appear."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bot.log")
        # Tiny limits so the test is fast: 1 KB per file, keep 3 backups
        main_mod._setup_logging(log_path=path, max_bytes=1024, backup_count=3)

        logger = logging.getLogger("test_rotation")
        # Each line ~80-100 bytes; 50 lines = ~4-5 KB → guarantees ≥3 rotations
        for i in range(50):
            logger.info(f"line {i:04d} " + "x" * 60)

        # Force the handler to flush
        for h in logging.getLogger().handlers:
            h.flush()

        # bot.log exists (the active file)
        assert os.path.exists(path), f"Active log {path} should exist"

        # At least one backup must exist (rotation happened)
        backups = sorted(
            f for f in os.listdir(tmp)
            if f.startswith("bot.log.") and f != "bot.log"
        )
        assert len(backups) >= 1, (
            f"Expected ≥1 backup file after rotation; found {os.listdir(tmp)}"
        )


def test_backup_count_caps_total_files():
    """Rotation never keeps more than backup_count + 1 files (active + backups)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "bot.log")
        backup_count = 2
        main_mod._setup_logging(
            log_path=path, max_bytes=1024, backup_count=backup_count
        )

        logger = logging.getLogger("test_backup_cap")
        # Generate way more rotations than backup_count to force pruning
        for i in range(200):
            logger.info(f"line {i:04d} " + "x" * 60)

        for h in logging.getLogger().handlers:
            h.flush()

        log_files = [
            f for f in os.listdir(tmp)
            if f == "bot.log" or f.startswith("bot.log.")
        ]
        assert len(log_files) <= backup_count + 1, (
            f"Expected ≤{backup_count + 1} log files (1 active + {backup_count} backups); "
            f"found {len(log_files)}: {sorted(log_files)}"
        )
