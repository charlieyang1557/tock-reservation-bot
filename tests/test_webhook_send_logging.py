"""Tier-1 CHANGE 2 — webhook delivery observability.

Before this change a Discord send was invisible on success (no log line at
all) and near-invisible on failure, so the operator could not tell "embed
never fired" apart from "embed fired but Discord rejected/dropped it"
(06/26 postmortem: silent alerting failures compounded the silent idle).

Pinned here:
  - 2xx → '[notify] webhook delivered (<status>) — <embed title>' at INFO
  - non-2xx → WARNING naming the status, the embed title, and the first
    ~200 chars of the response body
  - network exception → WARNING with the exception repr (class name visible)
  - the send NEVER raises into the caller (fire-and-forget preserved)

The HTTP layer is mocked by swapping a fake `aiohttp` module into
sys.modules — _send_discord does `import aiohttp` inside the function, so
the fake is picked up without touching the real package.
"""
import asyncio
import logging
import sys
import types
from unittest.mock import MagicMock

import pytest

from src.config import Config
from src.notifier import Notifier


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, card_cvc="",
        discord_webhook_url="https://discord.invalid/webhook",
    )
    defaults.update(overrides)
    return Config(**defaults)


class _FakeResponse:
    def __init__(self, status: int, body: str):
        self.status = status
        self._body = body

    async def text(self) -> str:
        return self._body


class _FakeRequestCtx:
    """Async context manager returned by session.post(...)."""

    def __init__(self, response: _FakeResponse | None, exc: Exception | None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        if self._exc is not None:
            raise self._exc
        return self._response

    async def __aexit__(self, *args):
        return False


class _FakeSession:
    def __init__(self, response: _FakeResponse | None, exc: Exception | None):
        self._response = response
        self._exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return _FakeRequestCtx(self._response, self._exc)


def _install_fake_aiohttp(
    monkeypatch, *, status: int = 204, body: str = "",
    exc: Exception | None = None,
):
    """Swap a stub aiohttp module into sys.modules for this test."""
    response = None if exc is not None else _FakeResponse(status, body)
    fake = types.ModuleType("aiohttp")
    fake.ClientSession = lambda *a, **k: _FakeSession(response, exc)
    fake.ClientTimeout = lambda **k: MagicMock()
    monkeypatch.setitem(sys.modules, "aiohttp", fake)


def _send(notifier: Notifier, title: str = "🎯 Test Embed") -> None:
    asyncio.run(notifier._send_discord(title, "desc", 0xFF0000, []))


# ---------------------------------------------------------------------------
# 2xx success logging
# ---------------------------------------------------------------------------

def test_204_logs_delivered_with_title(monkeypatch, caplog):
    _install_fake_aiohttp(monkeypatch, status=204)
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier, title="🎯 Sniper Mode Active")

    assert "webhook delivered" in caplog.text
    assert "204" in caplog.text
    assert "🎯 Sniper Mode Active" in caplog.text


def test_200_also_counts_as_delivered(monkeypatch, caplog):
    _install_fake_aiohttp(monkeypatch, status=200)
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier)

    assert "webhook delivered" in caplog.text
    assert "200" in caplog.text


def test_2xx_does_not_warn(monkeypatch, caplog):
    _install_fake_aiohttp(monkeypatch, status=204)
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier)

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == []


# ---------------------------------------------------------------------------
# non-2xx warning
# ---------------------------------------------------------------------------

def test_non_2xx_warns_with_status_title_and_body(monkeypatch, caplog):
    _install_fake_aiohttp(
        monkeypatch, status=429, body="rate limited: retry later " * 20
    )
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier, title="❌ Booking Failed — 2 slot(s)")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    assert "429" in msg
    assert "❌ Booking Failed — 2 slot(s)" in msg
    assert "rate limited: retry later" in msg
    # Body is truncated to ~200 chars so a huge error page can't flood the log
    assert len(msg) < 500


def test_non_2xx_never_logs_delivered(monkeypatch, caplog):
    _install_fake_aiohttp(monkeypatch, status=404, body="Unknown Webhook")
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier)

    assert "webhook delivered" not in caplog.text


# ---------------------------------------------------------------------------
# network exception
# ---------------------------------------------------------------------------

def test_network_exception_warns_with_repr_and_title(monkeypatch, caplog):
    _install_fake_aiohttp(monkeypatch, exc=ConnectionError("boom"))
    notifier = Notifier(_make_config())

    with caplog.at_level(logging.DEBUG, logger="src.notifier"):
        _send(notifier, title="✅ Reservation Confirmed!")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    msg = warnings[0].getMessage()
    # repr(e) so the exception CLASS is visible, not just its (often empty) str
    assert "ConnectionError" in msg
    assert "boom" in msg
    assert "✅ Reservation Confirmed!" in msg


def test_send_never_raises_into_caller(monkeypatch):
    """Fire-and-forget contract: neither HTTP errors nor network exceptions
    may propagate out of _send_discord."""
    _install_fake_aiohttp(monkeypatch, exc=RuntimeError("socket exploded"))
    notifier = Notifier(_make_config())

    # Must not raise
    _send(notifier)

    _install_fake_aiohttp(monkeypatch, status=500, body="oops")
    _send(notifier)
