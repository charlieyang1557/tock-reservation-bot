"""Tests for Codex review fixes on the calendar_replay integration.

HIGH 1: Replay can return 100+ slots (vs ~3 from per-date scan). The
booker races every slot → up to N concurrent booking pages → Tock ban
risk. Fix: cap replay output to top K per date.

HIGH 2: Tock could return 200 + HTML CF interstitial. parse() returns
[] → check_all treats as "no slots success" → legacy fallback skipped
during a release window → potential missed slots. Fix: detect non-
protobuf bodies (HTML signature, too-small body) and treat as failure
(None) so legacy path runs.

MEDIUM 4: Replay failures need to be counted/logged separately so the
operator can spot stale-auth situations. Fix: track replay attempts
+ failures per sniper window; circuit-break after N consecutive
failures so we stop hammering Tock with bad replays.
"""
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# HIGH 1: cap replay slots
# ---------------------------------------------------------------------------

def test_cap_slots_per_date_default_keeps_top_3_per_date():
    """The cap helper keeps at most 3 slots per date by default
    (closest to preferred_time)."""
    from src.calendar_replay import cap_slots_per_date
    from src.checker import AvailableSlot

    slots = [
        AvailableSlot(slot_date=date(2026, 5, 15), slot_time="5:00 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 15), slot_time="5:30 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 15), slot_time="6:00 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 15), slot_time="8:30 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 15), slot_time="11:00 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 22), slot_time="5:00 PM",
                      day_of_week="Friday"),
        AvailableSlot(slot_date=date(2026, 5, 22), slot_time="6:00 PM",
                      day_of_week="Friday"),
    ]
    capped = cap_slots_per_date(slots, preferred_time="17:00", per_date_cap=3)
    by_date = {}
    for s in capped:
        by_date.setdefault(s.slot_date.isoformat(), []).append(s.slot_time)
    # Each date capped to 3 max
    assert all(len(v) <= 3 for v in by_date.values())
    # 5/15 had 5 slots → 3 closest to 17:00 should be 5:00, 5:30, 6:00 PM
    assert set(by_date["2026-05-15"]) == {"5:00 PM", "5:30 PM", "6:00 PM"}


def test_cap_slots_total_limit_protects_against_huge_responses():
    """Even with 6 dates × 5 slots = 30, the total cap keeps us under
    a hammer threshold (e.g., 18 max = 6 dates × 3 per date)."""
    from src.calendar_replay import cap_slots_per_date
    from src.checker import AvailableSlot
    from datetime import timedelta

    slots = []
    for d in range(6):
        for h in (17, 17, 18, 18, 19):  # 5 slots per date
            mm = (h % 2) * 30
            slots.append(AvailableSlot(
                slot_date=date(2026, 5, 15) + timedelta(days=d),
                slot_time=f"{h % 12 or 12}:{mm:02d} PM",
                day_of_week="Friday",
            ))
    assert len(slots) == 30
    capped = cap_slots_per_date(slots, preferred_time="17:00", per_date_cap=3)
    assert len(capped) <= 18, (
        f"6 dates × 3 per date = max 18; got {len(capped)} — "
        "hammering risk"
    )


# ---------------------------------------------------------------------------
# HIGH 2: detect non-protobuf 200 (CF HTML interstitial)
# ---------------------------------------------------------------------------

def test_looks_like_cloudflare_html_interstitial():
    """The CF interstitial HTML has signature markers we must detect
    even when status is 200."""
    from src.calendar_replay import body_looks_protobuf

    cf_body = (
        b"<!DOCTYPE html><html><head><title>Just a moment...</title>"
        b"<script>...</script></head><body><div class='cf-turnstile'>"
        b"</div></body></html>"
    )
    assert body_looks_protobuf(cf_body) is False


def test_looks_like_protobuf_for_real_tock_body():
    """A real Tock calendar/full body starts with protobuf framing
    bytes and is large (>500 b for a non-empty calendar)."""
    from src.calendar_replay import body_looks_protobuf

    # Real benu body shape: starts with framing bytes, contains date markers
    body = (
        b"\x0a\xef\xbf\xbd\xef\xbf\xbd\x01" +
        b"\x0a\x0a2026-05-15\x12\x05\x1a\x0517:30\x00" * 50  # repeat to bulk
    )
    assert len(body) > 500
    assert body_looks_protobuf(body) is True


def test_looks_like_protobuf_for_empty_calendar_body():
    """An empty calendar (sold out — 0 dates) MIGHT be small but should
    still look protobuf-ish (binary, not HTML). The detector errs on
    the side of "looks protobuf" for ambiguous binary blobs."""
    from src.calendar_replay import body_looks_protobuf

    # Sold-out shape: just framing, no dates
    body = b"\x0a\x00\x12\x00"
    # A small binary body is NOT confirmable; safer to call this "not
    # protobuf-shaped enough" → legacy fallback runs as defense
    assert body_looks_protobuf(body) is False


@pytest.mark.asyncio
async def test_fetch_calendar_returns_none_for_html_body(monkeypatch):
    """When fetch returns 200 + HTML body (CF interstitial), the
    fetch_calendar wrapper should return None to trigger fallback."""
    from src.calendar_replay import fetch_calendar, CalendarReplaySession
    import base64

    cf_body = (
        b"<!DOCTYPE html><html>Just a moment...</html>"
    )
    cf_b64 = base64.b64encode(cf_body).decode()

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock(return_value={
        "status": 200,
        "body_b64": cf_b64,
        "body_len": len(cf_body),
        "elapsed_ms": 100,
    })
    sess = CalendarReplaySession(
        page=page,
        url="https://www.exploretock.com/api/consumer/calendar/full/v2",
        headers={},
        body_bytes=b"\xda\xba\x1d\x00",
        restaurant_slug="benu",
    )

    result = await fetch_calendar(sess)
    assert result is None, (
        "fetch_calendar must return None when body is HTML (likely "
        "CF interstitial) so check_all falls back to legacy path"
    )
    assert sess.consecutive_failures > 0


# ---------------------------------------------------------------------------
# MEDIUM 4: replay-failure tracking + circuit breaker
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_check_all_circuit_breaks_after_repeated_replay_failures(monkeypatch):
    """If replay fails N consecutive times, _try_calendar_replay should
    short-circuit return None WITHOUT even attempting fetch (saves the
    init+fetch cost on a known-broken path)."""
    from src.checker import AvailabilityChecker
    from src.config import Config

    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=True,
    )
    checker = AvailabilityChecker(cfg, MagicMock(), MagicMock())

    init_calls = []
    async def always_fail_init(*args, **kwargs):
        init_calls.append(True)
        return None
    async def fake_fetch(s):
        return None

    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", always_fail_init
    )
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", fake_fetch)
    from datetime import timedelta
    target = date.today() + timedelta(days=14)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [target] if "Friday" in days else [],
    )

    # First N attempts try init each time
    for _ in range(3):
        result = await checker._try_calendar_replay(sniper_mode=True)
        assert result is None
    assert len(init_calls) >= 1

    # After enough failures, the circuit should open and skip init
    # entirely. We expect _replay_disabled_until_window_end (or similar
    # sentinel) to be set.
    init_calls.clear()
    for _ in range(3):
        await checker._try_calendar_replay(sniper_mode=True)
    # Once circuit-broken, no more init attempts (or at most a small
    # bounded number)
    assert len(init_calls) <= 2, (
        f"Circuit should open after repeated failures; got {len(init_calls)} "
        "more init attempts after the breaker should have tripped"
    )


@pytest.mark.asyncio
async def test_check_all_resets_circuit_on_close_replay_session(monkeypatch):
    """close_replay_session() should ALSO reset the circuit-breaker
    state so the next sniper window starts fresh."""
    from src.checker import AvailabilityChecker
    from src.config import Config

    cfg = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=True,
    )
    checker = AvailabilityChecker(cfg, MagicMock(), MagicMock())
    # Manually trip the breaker
    checker._replay_circuit_open = True

    await checker.close_replay_session()
    # Circuit should be closed (reset) so next window can try replay again
    assert checker._replay_circuit_open is False
