"""Tier-1 CHANGE 1 (06/26 incident) — a PRE-EXISTING booking_uncertain.json
must refuse LOUDLY and must NOT kill the monitor.

06/26 23:30: a resurfaced slot produced an unverifiable confirm click and
booking_uncertain.json was written. From then on book_best_slot_race returned
UNVERIFIED_CONFIRM before creating any task; the monitor mapped that to
_booking_secured=True with logger.error ONLY (no Discord), and every later
poll early-returned — silent permanent idle.

New semantics pinned here:
  - Pre-existing guard file → BookingOutcome.REFUSED_UNCERTAIN_GUARD.
    Monitor keeps scanning (the file is re-read at each race start, so
    deleting it mid-window re-enables booking on the very next poll) and
    fires ONE deduped critical RED Discord embed
    (notifier.booking_refused_uncertain, carrying the file payload).
  - FRESH in-session unverified confirm → still UNVERIFIED_CONFIRM; the
    monitor still idles (_booking_secured=True). Unchanged, regression-pinned.
"""
import asyncio
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot
from src.config import Config
from src.monitor import TockMonitor
from src.notifier import Notifier, _RED


# ---------------------------------------------------------------------------
# Builders (mirror tests/test_race_all_slots.py + test_sniper_calendar_failure_alert.py)
# ---------------------------------------------------------------------------

def _make_config(**overrides) -> Config:
    defaults = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    defaults.update(overrides)
    return Config(**defaults)


def _make_booker(**config_overrides):
    from src.booker import TockBooker
    return TockBooker(_make_config(**config_overrides), MagicMock(), MagicMock())


def _make_monitor(**config_overrides) -> TockMonitor:
    cfg = _make_config(**config_overrides)
    browser = MagicMock()
    browser.warm_session = AsyncMock()

    checker = MagicMock()
    checker.check_all = AsyncMock(return_value=[])
    checker.close_sniper_pages = AsyncMock()
    checker.close_replay_session = AsyncMock()
    checker.close_handoff_pages = AsyncMock()
    checker.pop_handoff_page = MagicMock(return_value=None)
    checker.last_checks = 0
    checker.last_errors = 0

    notifier = MagicMock()
    tracker = MagicMock()

    return TockMonitor(cfg, browser, checker, notifier, tracker)


def _make_slot():
    return AvailableSlot(
        slot_date=date(2026, 7, 3), slot_time="5:00 PM", day_of_week="Friday",
    )


def _make_uncertain():
    from src.booking_uncertain import UncertainBooking
    return UncertainBooking(
        slot_date_str="2026-07-03",
        slot_time="5:00 PM",
        day_of_week="Friday",
        detected_at_iso=datetime(2026, 6, 26, 23, 30, 12).isoformat(),
    )


# ---------------------------------------------------------------------------
# Booker: pre-existing guard file → REFUSED_UNCERTAIN_GUARD, zero tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_persisted_guard_returns_refused_outcome_and_zero_tasks():
    """booking_uncertain.json on disk (simulated via read_uncertain patch)
    must return REFUSED_UNCERTAIN_GUARD — NOT UNVERIFIED_CONFIRM — and never
    fire a single _book_single task."""
    from src.booker import BookingOutcome

    booker = _make_booker()
    slots = [_make_slot()]
    attempts = []

    async def fake_book_single(slot, booking_won, **_kwargs):
        attempts.append(slot)
        return True

    with patch("src.booking_uncertain.read_uncertain",
               return_value=_make_uncertain()), \
         patch.object(booker, "_book_single", side_effect=fake_book_single):
        outcome, returned_slot = await booker.book_best_slot_race(slots)

    assert attempts == [], (
        f"Guarded race must not create any booking task; got {len(attempts)}"
    )
    assert outcome == BookingOutcome.REFUSED_UNCERTAIN_GUARD
    assert returned_slot is not None
    assert returned_slot.slot_date_str == "2026-07-03"
    assert returned_slot.slot_time == "5:00 PM"


@pytest.mark.asyncio
async def test_persisted_guard_passes_file_payload_through():
    """The guard block hands the UncertainBooking payload (date/time/
    detected_at) to the monitor via booker.last_refused_uncertain so the
    Discord embed can show WHICH confirm is unverified."""
    booker = _make_booker()
    persisted = _make_uncertain()

    with patch("src.booking_uncertain.read_uncertain", return_value=persisted):
        await booker.book_best_slot_race([_make_slot()])

    assert booker.last_refused_uncertain == persisted


@pytest.mark.asyncio
async def test_refused_payload_reset_when_guard_clear():
    """A stale last_refused_uncertain from a prior refused race must not leak
    into a later race that runs with the file cleared."""
    booker = _make_booker()
    booker.last_refused_uncertain = _make_uncertain()  # stale

    async def fake_book_single(slot, booking_won, **_kwargs):
        booking_won.set()
        return True

    with patch("src.booking_uncertain.read_uncertain", return_value=None), \
         patch.object(booker, "_book_single", side_effect=fake_book_single):
        await booker.book_best_slot_race([_make_slot()])

    assert booker.last_refused_uncertain is None


# ---------------------------------------------------------------------------
# Booker: FRESH in-session unverified confirm still UNVERIFIED_CONFIRM
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fresh_soft_win_during_race_still_returns_unverified_confirm():
    """The post-race path (a confirm clicked THIS race but unverifiable) is
    NOT the guard path — it must keep returning UNVERIFIED_CONFIRM."""
    from src.booker import BookingOutcome

    booker = _make_booker()
    slot = _make_slot()

    async def fake_book_single(s, booking_won, **_kwargs):
        # Simulate _book_single's soft-win path: confirm clicked, unverified.
        booker._unverified_confirm_slot = s
        return False

    with patch("src.booking_uncertain.read_uncertain", return_value=None), \
         patch.object(booker, "_book_single", side_effect=fake_book_single):
        outcome, returned_slot = await booker.book_best_slot_race([slot])

    assert outcome == BookingOutcome.UNVERIFIED_CONFIRM
    assert returned_slot == slot


@pytest.mark.asyncio
async def test_in_memory_guard_still_returns_unverified_confirm():
    """Same-process in-memory guard (file write may have failed) is also not
    the persisted-file path — stays UNVERIFIED_CONFIRM."""
    from src.booker import BookingOutcome

    booker = _make_booker()
    slot = _make_slot()
    booker._unverified_confirm_slot = slot  # prior race's soft-win

    attempts = []

    async def fake_book_single(s, booking_won, **_kwargs):
        attempts.append(s)
        return True

    with patch("src.booking_uncertain.read_uncertain", return_value=None), \
         patch.object(booker, "_book_single", side_effect=fake_book_single):
        outcome, returned_slot = await booker.book_best_slot_race([slot])

    assert attempts == []
    assert outcome == BookingOutcome.UNVERIFIED_CONFIRM
    assert returned_slot == slot


# ---------------------------------------------------------------------------
# Monitor: REFUSED_UNCERTAIN_GUARD branch
# ---------------------------------------------------------------------------

def _refused_monitor():
    """Monitor whose poll finds a slot and whose booker refuses (guard file)."""
    from src.booker import BookingOutcome

    m = _make_monitor()
    slot = _make_slot()
    m.checker.check_all = AsyncMock(return_value=[slot])
    m.booker.book_best_slot_race = AsyncMock(
        return_value=(BookingOutcome.REFUSED_UNCERTAIN_GUARD, slot)
    )
    m.booker.last_refused_uncertain = _make_uncertain()
    return m


def test_monitor_refused_does_not_secure_and_fires_embed_with_payload():
    """REFUSED must NOT set _booking_secured (monitor keeps scanning) and
    must fire the new Discord embed with the uncertain-file payload."""
    m = _refused_monitor()

    asyncio.run(m._poll_inner(sniper_age=None))

    assert m._booking_secured is False, (
        "REFUSED_UNCERTAIN_GUARD must not idle the monitor — the guard file "
        "is re-read at each race start, so the next poll can book once the "
        "operator deletes it"
    )
    m.notifier.booking_refused_uncertain.assert_called_once()
    payload = m.notifier.booking_refused_uncertain.call_args.args[0]
    assert payload.slot_date_str == "2026-07-03"
    assert payload.slot_time == "5:00 PM"


def test_monitor_refused_keeps_attempting_next_poll():
    """Polling continues after a refusal: the next poll still runs a race
    (which re-reads the guard file)."""
    m = _refused_monitor()

    asyncio.run(m._poll_inner(sniper_age=None))
    asyncio.run(m._poll_inner(sniper_age=None))

    assert m.booker.book_best_slot_race.await_count == 2


def test_monitor_refused_embed_deduped_across_consecutive_refusals():
    """A hot sniper window (~2 polls/s, each detecting slots and refusing)
    must fire ONE embed, not dozens."""
    m = _refused_monitor()

    asyncio.run(m._poll_inner(sniper_age=None))
    asyncio.run(m._poll_inner(sniper_age=None))
    asyncio.run(m._poll_inner(sniper_age=None))

    m.notifier.booking_refused_uncertain.assert_called_once()


def test_monitor_refused_embed_rearms_on_empty_poll():
    """Mirrors the _last_booking_failed_key pattern: an empty poll re-arms
    the dedup, so a later refusal alerts again."""
    m = _refused_monitor()
    slot = _make_slot()

    asyncio.run(m._poll_inner(sniper_age=None))          # refusal #1 → embed
    m.checker.check_all = AsyncMock(return_value=[])
    asyncio.run(m._poll_inner(sniper_age=None))          # empty poll → re-arm
    m.checker.check_all = AsyncMock(return_value=[slot])
    asyncio.run(m._poll_inner(sniper_age=None))          # refusal #2 → embed

    assert m.notifier.booking_refused_uncertain.call_count == 2


def test_monitor_refused_dedup_rearms_on_sniper_window_entry():
    """Mirrors the _last_booking_failed_key pattern: entering a new sniper
    window resets the dedup key."""
    m = _make_monitor()
    m._sniper_active = False
    m._last_refused_uncertain_key = ("2026-07-03", "5:00 PM", "stale")

    with patch.object(m, "_sniper_window_info", return_value="20:10"):
        m._get_poll_interval()

    assert m._last_refused_uncertain_key is None


def test_monitor_unverified_confirm_branch_unchanged():
    """Regression: a FRESH unverified confirm still idles the session
    (_booking_secured=True) and does NOT fire the refused embed."""
    from src.booker import BookingOutcome

    m = _make_monitor()
    slot = _make_slot()
    m.checker.check_all = AsyncMock(return_value=[slot])
    m.booker.book_best_slot_race = AsyncMock(
        return_value=(BookingOutcome.UNVERIFIED_CONFIRM, slot)
    )

    asyncio.run(m._poll_inner(sniper_age=None))

    assert m._booking_secured is True
    m.notifier.booking_refused_uncertain.assert_not_called()


# ---------------------------------------------------------------------------
# Notifier: booking_refused_uncertain embed
# ---------------------------------------------------------------------------

def test_notifier_refused_embed_is_critical_red_with_instructions():
    notifier = Notifier(_make_config())
    notifier._fire = MagicMock()

    notifier.booking_refused_uncertain(_make_uncertain())

    notifier._fire.assert_called_once()
    kwargs = notifier._fire.call_args.kwargs
    assert kwargs["color"] == _RED
    assert kwargs["critical"] is True
    desc = kwargs["description"]
    # The uncertain slot from the file payload
    assert "2026-07-03" in desc
    assert "5:00 PM" in desc
    assert "2026-06-26T23:30:12" in desc
    # Operator instructions
    assert "booking_uncertain.json" in desc
    assert "https://www.exploretock.com/account/reservations" in desc
    # Review P2: the embed must name the RESOLVED guard-file path (the file is
    # cwd-relative), not a hardcoded absolute path — a wrong path sends the
    # drop-night operator to delete a nonexistent file.
    from src.booking_uncertain import UNCERTAIN_FILE
    assert str(UNCERTAIN_FILE.resolve()) in desc
    assert "REFUSED" in kwargs["title"] or "REFUSED" in desc


def test_notifier_refused_embed_handles_none_payload():
    """Defensive: a missing payload (should not happen) must not crash the
    monitor's notification path."""
    notifier = Notifier(_make_config())
    notifier._fire = MagicMock()

    notifier.booking_refused_uncertain(None)

    notifier._fire.assert_called_once()
    kwargs = notifier._fire.call_args.kwargs
    assert kwargs["color"] == _RED
    assert "booking_uncertain.json" in kwargs["description"]


# ---------------------------------------------------------------------------
# --test-notify sequence includes the new embed
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_test_notify_sequence_includes_refused_embed():
    notifier = Notifier(_make_config(
        discord_webhook_url="https://discord.invalid/webhook"
    ))
    notifier._send_discord = AsyncMock()

    await notifier.test_all_notifications()

    titles = [c.args[0] for c in notifier._send_discord.call_args_list]
    assert any("REFUSED" in t for t in titles), (
        f"--test-notify must exercise the booking_refused_uncertain embed; "
        f"sent titles: {titles}"
    )
