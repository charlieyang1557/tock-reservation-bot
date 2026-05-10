"""Tests for event-driven slot detection telemetry (Phase B3.2 — first pass).

The full B3.2 plan asks for `page.expect_response` wrapping the day-click,
parsing the slot JSON directly to skip DOM. That requires the operator
to first discover Tock's actual XHR URL pattern + JSON shape via
headed-mode `--test-booking-flow`. Without that data the JSON parser
would be speculative — and a wrong parser could MISS slots, which is
worse than slow.

This first pass ships the infrastructure:
  - Config flag `event_driven_detection: bool = False` (default OFF)
  - Optional `event_driven_url_pattern: str = ""` (operator-tunable)
  - When enabled, register a Playwright response listener during the
    day-click that records every matching XHR to `xhr_telemetry.jsonl`
  - Existing DOM-based detection still runs (no behavior change for the
    booking flow itself)

Operator workflow:
  1. Set `EVENT_DRIVEN_DETECTION=true` in .env
  2. Run a real release-window cycle
  3. Inspect `xhr_telemetry.jsonl` to identify the slot-availability XHR
  4. Set `EVENT_DRIVEN_URL_PATTERN=...` to narrow the recording
  5. (future) Plug in a JSON parser to skip DOM entirely
"""
import asyncio
import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Config: flag and pattern wiring
# ---------------------------------------------------------------------------

def test_config_has_event_driven_flag_default_off():
    """Config carries the new flag; default keeps existing DOM-only path."""
    from src.config import Config
    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    assert hasattr(cfg, "event_driven_detection")
    assert cfg.event_driven_detection is False
    assert hasattr(cfg, "event_driven_url_pattern")
    assert cfg.event_driven_url_pattern == ""


def test_config_can_enable_event_driven_with_pattern():
    """Operator can opt in with a tuned URL pattern."""
    from src.config import Config
    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        event_driven_detection=True,
        event_driven_url_pattern="/api/availability/",
    )
    assert cfg.event_driven_detection is True
    assert cfg.event_driven_url_pattern == "/api/availability/"


# ---------------------------------------------------------------------------
# XhrTelemetry recorder — the core testable unit
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_xhr_telemetry_records_matching_responses(tmp_path):
    """The recorder accumulates response metadata for any URL containing
    the configured pattern, then atomically appends to the JSONL file."""
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="availability",
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    # Simulate Playwright responses
    fake_responses = [
        MagicMock(
            url="https://api.exploretock.com/v1/availability/restaurant?date=2026-05-15",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
        MagicMock(
            url="https://www.exploretock.com/static/style.css",
            status=200,
            request=MagicMock(method="GET", resource_type="stylesheet"),
        ),
        MagicMock(
            url="https://api.exploretock.com/v1/availability/other?date=2026-05-15",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
    ]
    for r in fake_responses:
        recorder._on_response(r)

    await recorder.flush()

    assert log_path.exists()
    lines = log_path.read_text().strip().splitlines()
    # Two of three responses match the "availability" pattern
    assert len(lines) == 2
    for line in lines:
        rec = json.loads(line)
        assert "availability" in rec["url"]
        assert rec["status"] == 200


@pytest.mark.asyncio
async def test_xhr_telemetry_default_pattern_includes_target_date(tmp_path):
    """Default pattern (when operator hasn't set EVENT_DRIVEN_URL_PATTERN)
    matches URLs containing the target date — the most reliable
    heuristic without operator pre-work."""
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="",  # empty → use date heuristic
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    fake_responses = [
        MagicMock(
            url="https://api.exploretock.com/anything?date=2026-05-15",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
        MagicMock(
            url="https://api.exploretock.com/anything?date=2026-05-22",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
    ]
    for r in fake_responses:
        recorder._on_response(r)
    await recorder.flush()

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert "2026-05-15" in rec["url"]


@pytest.mark.asyncio
async def test_xhr_telemetry_does_not_record_failed_responses(tmp_path):
    """Non-2xx responses are noise — skip them to keep the JSONL useful."""
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="availability",
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    fake_responses = [
        MagicMock(
            url="https://api.exploretock.com/availability?date=2026-05-15",
            status=503,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
        MagicMock(
            url="https://api.exploretock.com/availability?date=2026-05-15",
            status=200,
            request=MagicMock(method="GET", resource_type="xhr"),
        ),
    ]
    for r in fake_responses:
        recorder._on_response(r)
    await recorder.flush()

    lines = log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["status"] == 200


@pytest.mark.asyncio
async def test_xhr_telemetry_handles_response_with_missing_attrs(tmp_path):
    """Defensive: if a Response object lacks expected attributes
    (mocks, future Playwright versions), skip it without crashing."""
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="availability",
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    # A response that raises on attribute access
    bad_response = MagicMock()
    type(bad_response).url = property(
        lambda self: (_ for _ in ()).throw(RuntimeError("no url"))
    )

    recorder._on_response(bad_response)  # must not raise
    await recorder.flush()

    # File may or may not exist — what matters is no crash and no record
    if log_path.exists():
        assert log_path.read_text().strip() == ""


@pytest.mark.asyncio
async def test_xhr_telemetry_idempotent_flush(tmp_path):
    """Calling flush() twice on an empty buffer must not duplicate
    existing records or crash."""
    from src.xhr_telemetry import XhrTelemetryRecorder

    log_path = tmp_path / "xhr_telemetry.jsonl"
    recorder = XhrTelemetryRecorder(
        url_pattern="availability",
        log_path=log_path,
        target_date=date(2026, 5, 15),
    )

    recorder._on_response(MagicMock(
        url="https://api.exploretock.com/availability?date=2026-05-15",
        status=200,
        request=MagicMock(method="GET", resource_type="xhr"),
    ))
    await recorder.flush()
    initial = log_path.read_text()

    await recorder.flush()  # nothing new in the buffer
    after = log_path.read_text()

    assert initial == after


# ---------------------------------------------------------------------------
# Integration: recorder used during _check_date when flag is on
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_date_does_not_use_recorder_when_flag_off(tmp_path):
    """Default (flag OFF): no XHR listener registered, no JSONL written."""
    from src.checker import AvailabilityChecker
    from src.config import Config

    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        event_driven_detection=False,  # explicitly off
    )
    checker = AvailabilityChecker(cfg, MagicMock(), MagicMock())

    page = AsyncMock()
    page.on = MagicMock()  # spy
    page.remove_listener = MagicMock()

    # If the flag is off, no listener should be registered
    # We test this via a helper method
    recorder = checker._maybe_create_xhr_recorder(page, date(2026, 5, 15))
    assert recorder is None, (
        "When event_driven_detection=False, _maybe_create_xhr_recorder "
        "must return None and not register any listener"
    )


@pytest.mark.asyncio
async def test_check_date_creates_recorder_when_flag_on(tmp_path, monkeypatch):
    """When the flag is on, _maybe_create_xhr_recorder returns a
    Recorder and registers a `response` listener on the page."""
    monkeypatch.chdir(tmp_path)  # so any default file paths are isolated

    from src.checker import AvailabilityChecker
    from src.config import Config

    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        event_driven_detection=True,
        event_driven_url_pattern="availability",
    )
    checker = AvailabilityChecker(cfg, MagicMock(), MagicMock())

    page = AsyncMock()
    page.on = MagicMock()
    page.remove_listener = MagicMock()

    recorder = checker._maybe_create_xhr_recorder(page, date(2026, 5, 15))

    assert recorder is not None
    page.on.assert_called_once()
    args, _ = page.on.call_args
    assert args[0] == "response"
