"""06/26 incident P0 (Tier-1, part 1) — start replay fetching BEFORE the release.

06/26 forensics: the pre-release gate in check_all returned [] with NO network
fetch while sniper_window_age_sec < 60, so the very first replay fetch fired at
exactly 20:00:00.0, raced the release commit, and read the pre-release body.

Fix under test: the no-fetch gate shrinks to
    age < _RELEASE_AT_WINDOW_AGE_SEC - _PRE_RELEASE_FETCH_LEAD_SEC  (= 45s),
so replay fetches begin at window age 45s (T-15s before the nominal release).
In the pre-release margin (45 <= age < 60):
  - the replay fetch runs (and its empty re-poll loop may run — extra fetches
    across the T0 boundary are the point), BUT
  - the multi-second DOM safety-net scan must NEVER run (pre-release empty is
    the expected state; a ~7s DOM scan straddling T0 would be catastrophic),
    even when replay returns None (fetch failure), and
  - the DOM safety-net throttle anchor is NOT armed (arming pre-release would
    let a scan fire at T+0 instead of the intended earliest ~T+10).
Any poll in the pre-fetch phase (age < 45) resets the throttle anchor so each
sniper window starts fresh.
"""
from datetime import date, timedelta
from unittest.mock import MagicMock

import pytest

from src.checker import AvailabilityChecker, AvailableSlot
from src.config import Config


# Window-age samples for each phase/boundary.
PRE_FETCH_AGE = 44.9          # just below the fetch-start boundary
MARGIN_START_AGE = 45.0       # first age at which replay fetches run
MARGIN_END_AGE = 59.9         # last age of the pre-release margin
RELEASE_AGE = 60.0            # first post-release age


@pytest.fixture(autouse=True)
def _fast_repoll(monkeypatch):
    """The margin/empty paths drive the replay re-poll loop; zero the sleep so
    tests stay fast (same pattern as test_replay_repoll_latency.py)."""
    monkeypatch.setattr("src.checker._REPLAY_REPOLL_INTERVAL_SEC", 0)


def _make_checker(use_replay: bool = True):
    cfg = Config(
        tock_email="t@t.com", tock_password="pw",
        restaurant_slug="fui-hui-hua-san-francisco",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="20:00", scan_weeks=2, dry_run=True, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
        use_calendar_replay=use_replay,
    )
    return AvailabilityChecker(cfg, MagicMock(), MagicMock())


def _slot(d: date, t: str) -> AvailableSlot:
    return AvailableSlot(slot_date=d, slot_time=t, day_of_week=d.strftime("%A"))


def _single_target(checker, target: date):
    checker._get_target_dates = (  # type: ignore[method-assign]
        lambda days, sniper_mode=False: [target] if "Friday" in days else []
    )


def _wire(checker, monkeypatch, replay_result):
    """Wire spies: fake replay returning *replay_result* each call, fake DOM
    _check_date. Returns (replay_calls, dom_calls) lists."""
    target = date.today() + timedelta(days=14)
    _single_target(checker, target)

    replay_calls: list[bool] = []
    async def fake_replay(*args, **kwargs):
        replay_calls.append(True)
        return replay_result
    monkeypatch.setattr(checker, "_try_calendar_replay", fake_replay)

    dom_calls: list[date] = []
    async def fake_check_date(d, **kwargs):
        dom_calls.append(d)
        return []
    monkeypatch.setattr(checker, "_check_date", fake_check_date)
    return replay_calls, dom_calls


# ---------------------------------------------------------------------------
# Boundary: pre-fetch phase (age < 45) — no fetch of ANY kind
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_fetch_at_all_below_fetch_start_boundary(monkeypatch):
    """age=44.9: still the no-I/O phase — neither replay nor DOM runs."""
    checker = _make_checker(use_replay=True)
    replay_calls, dom_calls = _wire(checker, monkeypatch, [])

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=PRE_FETCH_AGE,
    )
    assert result == []
    assert replay_calls == [], "no replay fetch before window age 45s"
    assert dom_calls == [], "no DOM scan before window age 45s"


# ---------------------------------------------------------------------------
# Boundary: margin start (age = 45) — replay fetches begin, DOM stays off
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_fetch_begins_at_margin_start(monkeypatch):
    """age=45.0: replay fetch fires (T-15s head start), DOM never runs."""
    checker = _make_checker(use_replay=True)
    replay_calls, dom_calls = _wire(checker, monkeypatch, [])

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_START_AGE,
    )
    assert result == []
    assert replay_calls, "replay fetch must run from window age 45s (T-15s)"
    assert dom_calls == [], "DOM scan must never run in the pre-release margin"


# ---------------------------------------------------------------------------
# Boundary: margin end (age = 59.9) — empty AND None both skip the DOM scan
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_dom_scan_in_margin_when_replay_empty(monkeypatch):
    """age=59.9, clean-empty replay: expected pre-release state — nothing to
    safety-net, so the ~7s DOM scan must NOT straddle T0."""
    checker = _make_checker(use_replay=True)
    replay_calls, dom_calls = _wire(checker, monkeypatch, [])

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_END_AGE,
    )
    assert result == []
    assert replay_calls, "replay fetch must run in the margin"
    assert dom_calls == [], "empty replay in the margin must NOT trigger DOM"


@pytest.mark.asyncio
async def test_no_dom_scan_in_margin_even_when_replay_fails(monkeypatch):
    """age=59.9, replay None (fetch/decode FAILURE): post-release this would
    bypass the DOM throttle, but pre-release the DOM scan must STILL never
    run — there is no release to detect yet."""
    checker = _make_checker(use_replay=True)
    replay_calls, dom_calls = _wire(checker, monkeypatch, None)

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_END_AGE,
    )
    assert result == []
    assert replay_calls, "replay fetch must still be attempted in the margin"
    assert dom_calls == [], "replay failure in the margin must NOT trigger DOM"


@pytest.mark.asyncio
async def test_margin_poll_does_not_arm_dom_safety_net_anchor(monkeypatch):
    """A margin poll must NOT initialize the DOM-throttle anchor: if it did,
    the first post-release clean-empty poll would see >=10s elapsed and fire
    the ~7s DOM scan at T+0 instead of the intended earliest ~T+10."""
    checker = _make_checker(use_replay=True)
    _wire(checker, monkeypatch, [])

    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_END_AGE,
    )
    assert checker._dom_safety_net_anchor is None


# ---------------------------------------------------------------------------
# Boundary: release (age = 60) — post-release semantics take over
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_replay_failure_at_release_boundary_runs_dom(monkeypatch):
    """age=60.0, replay None: post-release a replay FAILURE bypasses the DOM
    throttle (replay is broken; DOM is the only detection) — proves 60.0 is
    on the post-release side of the boundary."""
    checker = _make_checker(use_replay=True)
    replay_calls, dom_calls = _wire(checker, monkeypatch, None)

    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=RELEASE_AGE,
    )
    assert replay_calls
    assert dom_calls, "post-release replay failure must fall back to DOM"


# ---------------------------------------------------------------------------
# Margin fast path: a replay HIT in the margin is returned immediately
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_margin_replay_hit_returns_slots_immediately(monkeypatch):
    """If Tock flips early (or our clock is behind), a margin replay hit must
    be returned for booking immediately — DOM stays skipped (fast path)."""
    checker = _make_checker(use_replay=True)
    target = date.today() + timedelta(days=14)
    found = [_slot(target, "8:00 PM")]
    replay_calls, dom_calls = _wire(checker, monkeypatch, found)

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=50.0,
    )
    assert result == found
    assert len(replay_calls) == 1, "hit on first fetch — no re-poll needed"
    assert dom_calls == []


# ---------------------------------------------------------------------------
# Replay disabled: the old no-I/O gate stays at the release boundary (60s)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_margin_with_replay_off_keeps_old_no_fetch_gate(monkeypatch):
    """use_calendar_replay=False: there is nothing safe to fetch pre-release
    (the only fetch would be the DOM scan, which must not straddle T0), so
    the no-I/O gate stays at age < 60 exactly as before."""
    checker = _make_checker(use_replay=False)
    replay_calls, dom_calls = _wire(checker, monkeypatch, [])

    result = await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=50.0,
    )
    assert result == []
    assert replay_calls == []
    assert dom_calls == [], "replay-off margin poll must stay a no-op until 60s"


# ---------------------------------------------------------------------------
# Per-window reset: a pre-fetch-phase poll clears the DOM-throttle anchor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pre_fetch_phase_poll_resets_dom_safety_net_anchor(monkeypatch):
    """A poll at age < 45 marks a NEW window's quiet phase — any anchor left
    over from the previous window must be cleared so the next window's
    first post-release empty poll re-arms from scratch."""
    checker = _make_checker(use_replay=True)
    _wire(checker, monkeypatch, [])
    checker._dom_safety_net_anchor = 12345.0  # leftover from a prior window

    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=10.0,
    )
    assert checker._dom_safety_net_anchor is None


# ---------------------------------------------------------------------------
# Pre-deploy review fix (07/02): margin-phase replay failures must not leave
# the circuit breaker open at the release moment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_margin_failures_do_not_leave_circuit_open_at_release(monkeypatch):
    """A CF blip during T-15..T0 can trip the replay circuit breaker; the
    monitor's reset only fires at window ENTRY (age 0). The breaker must
    re-arm once when crossing the release boundary so the ~160ms fast path is
    live at T0 instead of the ~7s DOM-bypass path."""
    checker = _make_checker(use_replay=True)
    _wire(checker, monkeypatch, [])

    # A margin poll observes the pre-release phase (arms the boundary reset).
    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_END_AGE,
    )
    # Simulate margin fetch failures having opened the breaker.
    checker._replay_failure_count = 3
    checker._replay_circuit_open = True

    # First post-release poll: breaker must be re-armed before replay runs.
    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=RELEASE_AGE,
    )
    assert checker._replay_circuit_open is False, (
        "circuit must be re-armed at the release boundary"
    )
    assert checker._replay_failure_count == 0


@pytest.mark.asyncio
async def test_release_boundary_circuit_reset_fires_only_once(monkeypatch):
    """The boundary re-arm is one-shot: post-release failures must still be
    able to open the breaker without the next poll silently resetting it."""
    checker = _make_checker(use_replay=True)
    _wire(checker, monkeypatch, [])

    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=MARGIN_END_AGE,
    )
    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=RELEASE_AGE,
    )
    # Breaker opens AFTER the boundary (real post-release failures).
    checker._replay_failure_count = 3
    checker._replay_circuit_open = True

    await checker.check_all(
        concurrent=True, keep_pages=True, sniper_window_age_sec=RELEASE_AGE + 5,
    )
    assert checker._replay_circuit_open is True, (
        "post-release breaker trips must NOT be reset by later polls"
    )
