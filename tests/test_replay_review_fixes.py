"""Tests for the 2026-06-09 post-recalibration review fixes.

Review of commit 68707e6 (structural protobuf decode) confirmed four gaps:

  #1 decode_ok was never consulted: a body that failed strict top-level
     decode parsed to [] which _try_calendar_replay treated as success —
     normal-mode check_all returned it as authoritative "no slots" (no DOM
     fallback) and the circuit-breaker counter was reset.
  #2 Replay-detected slots (no target_selector, page never rendered the
     release) were handed a STALE sniper warm page from a previous poll;
     the booker clicked it without reload → guaranteed repeat of the 06/05
     detected-but-click-failed incident via the new fast path.
  #3 The recursive collector's return value was ignored: hitting
     _MAX_DECODE_DEPTH silently dropped subtrees while decode_ok stayed
     True — indistinguishable from sold-out.
  #4 No upper size bound before the synchronous decode: a hostile multi-MB
     200 body would stall the event loop mid-sniper-window.

Plus two hardenings from the same review: proto_fixtures.varint rejects
negatives (infinite loop), and parse coerces bytearray/memoryview input.
"""
import asyncio
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailableSlot
from src.config import Config
from tests.proto_fixtures import (
    calendar_body,
    date_section,
    len_field,
    sold_out_date_section,
    varint,
)

TARGET = date(2026, 5, 15)


def _deep_chain(levels: int = 40) -> bytes:
    """Valid protobuf nested deeper than _MAX_DECODE_DEPTH."""
    payload = len_field(1, b"x")
    for _ in range(levels):
        payload = len_field(1, payload)
    return payload


def _make_config(**overrides) -> Config:
    kwargs = dict(
        tock_email="t@e.com", tock_password="p", card_cvc="123",
        discord_webhook_url="", headless=True, dry_run=False,
        restaurant_slug="test", party_size=2,
        preferred_days=["Friday", "Saturday", "Sunday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=2,
        release_window_days=["Monday"], release_window_start="09:00",
        release_window_end="11:00", sniper_days=["Friday"],
        sniper_times=["19:59"], sniper_duration_min=11, sniper_interval_sec=3,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


def _make_replay_checker(monkeypatch, fetch_body: bytes):
    """Checker with replay enabled and the network layer stubbed so
    _try_calendar_replay exercises the REAL parse on fetch_body."""
    from src.checker import AvailabilityChecker

    cfg = _make_config()
    cfg.use_calendar_replay = True
    checker = AvailabilityChecker(cfg, MagicMock(), MagicMock())

    fake_session = MagicMock()
    fake_session.consecutive_failures = 0

    async def fake_init(*args, **kwargs):
        return fake_session

    async def fake_fetch(sess):
        return fetch_body

    monkeypatch.setattr(
        "src.calendar_replay.initialize_replay_session", fake_init
    )
    monkeypatch.setattr("src.calendar_replay.fetch_calendar", fake_fetch)
    monkeypatch.setattr(
        checker, "_get_target_dates",
        lambda days, sniper_mode=False: [TARGET] if "Friday" in days else [],
    )
    return checker


# --------------------------------------------------------------------------- #
# Fix #3 (parser layer) — depth-cap truncation is reported, not swallowed
# --------------------------------------------------------------------------- #

def test_diag_truncated_set_when_depth_cap_hit():
    from src.calendar_replay import parse_with_diagnostics

    slots, diag = parse_with_diagnostics(_deep_chain(), [TARGET])
    assert diag.truncated is True
    assert slots == []


def test_diag_truncated_false_for_real_release_capture():
    """The committed 06/05 capture nests well inside the cap — truncated
    must stay False (regression guard: the cap keeps headroom)."""
    import pathlib
    from src.calendar_replay import parse_with_diagnostics

    body = (
        pathlib.Path(__file__).parent / "fixtures"
        / "20260605T200009_780186_fui-hui-hua-san-francisco_replay.bin"
    ).read_bytes()
    slots, diag = parse_with_diagnostics(
        body, [date(2026, 6, d) for d in (5, 6, 7, 10, 11, 12, 13, 14)]
    )
    assert diag.truncated is False
    assert diag.decode_ok is True
    assert len(slots) == 3  # the 3 BOOKABLE (f5>0) seatings


def test_diag_truncated_false_for_simple_valid_body():
    from src.calendar_replay import parse_with_diagnostics

    body = calendar_body(sold_out_date_section("2026-05-15"))
    _, diag = parse_with_diagnostics(body, [TARGET])
    assert diag.truncated is False
    assert diag.decode_ok is True


def test_truncated_body_can_still_yield_slots():
    """Truncation only marks the diag — seatings decoded BEFORE the cap
    are still returned (partial detection beats none in the fast path)."""
    from src.calendar_replay import parse_with_diagnostics

    body = calendar_body(date_section("2026-05-15", ["17:00"]), _deep_chain())
    slots, diag = parse_with_diagnostics(body, [TARGET])
    assert diag.truncated is True
    assert [(s.slot_date_str, s.slot_time) for s in slots] == [
        ("2026-05-15", "5:00 PM")
    ]


# --------------------------------------------------------------------------- #
# Fix #1 (checker layer) — suspect-empty parse is a FAILURE, not "no slots"
# --------------------------------------------------------------------------- #

@pytest.mark.asyncio
async def test_decode_failure_returns_none_without_breaker_count(monkeypatch):
    """Body that fails strict decode → _try_calendar_replay returns None
    (caller falls back to DOM) and the bytes are dumped for calibration.
    It must NOT count toward the circuit breaker: the fetch succeeded, and
    the pre-release sold-out body has a different shape than the release
    body — opening the circuit on a parse anomaly would forfeit the fast
    path for the release moment (merge-review finding)."""
    body = b"\x0a\x0a2026-05-15" + b"\x00" * 300  # raw date + invalid tags
    checker = _make_replay_checker(monkeypatch, body)
    checker._capture_replay_miss = MagicMock()

    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result is None
    assert checker._replay_failure_count == 0
    checker._capture_replay_miss.assert_called_once()


@pytest.mark.asyncio
async def test_truncated_empty_parse_returns_none(monkeypatch):
    """Depth-cap truncation with zero slots is equally suspect → None."""
    checker = _make_replay_checker(monkeypatch, _deep_chain())
    checker._capture_replay_miss = MagicMock()

    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result is None
    assert checker._replay_failure_count == 0


@pytest.mark.asyncio
async def test_clean_sold_out_parse_still_returns_empty_list(monkeypatch):
    """A body that decodes cleanly to zero seatings is a REAL empty
    calendar: return [] (authoritative), reset the failure counter."""
    body = calendar_body(sold_out_date_section("2026-05-15"))
    checker = _make_replay_checker(monkeypatch, body)
    checker._replay_failure_count = 1  # must be reset by the success

    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result == []
    assert checker._replay_failure_count == 0


@pytest.mark.asyncio
async def test_truncated_with_slots_still_returns_slots(monkeypatch):
    """Truncation must never suppress seatings that DID decode."""
    body = calendar_body(date_section("2026-05-15", ["17:00"]), _deep_chain())
    checker = _make_replay_checker(monkeypatch, body)

    result = await checker._try_calendar_replay(sniper_mode=False)
    assert result is not None
    assert [(s.slot_date_str, s.slot_time) for s in result] == [
        ("2026-05-15", "5:00 PM")
    ]


@pytest.mark.asyncio
async def test_suspect_empty_never_opens_circuit(monkeypatch):
    """Persistent suspect-empty parses must keep RETRYING replay every
    poll (DOM fallback covers each one) — pre-release sniper polls run at
    0s interval, so any breaker here would open within ~2 seconds and
    disable the ~160ms fast path for the release moment itself."""
    body = b"\x0a\x0a2026-05-15" + b"\x00" * 300
    checker = _make_replay_checker(monkeypatch, body)
    checker._capture_replay_miss = MagicMock()

    for _ in range(checker._REPLAY_FAILURE_THRESHOLD + 2):
        result = await checker._try_calendar_replay(sniper_mode=False)
        assert result is None
    assert checker._replay_circuit_open is False


@pytest.mark.asyncio
async def test_parse_exception_opens_circuit_and_dumps_body(monkeypatch):
    """Persistent parse EXCEPTIONS (a crashing parser, distinct from
    suspect-empty) still open the breaker — and the body that crashes the
    parser is dumped for offline calibration BEFORE the circuit kills all
    further fetches (merge-review finding: this branch lost the evidence)."""
    body = calendar_body(sold_out_date_section("2026-05-15"))
    checker = _make_replay_checker(monkeypatch, body)
    checker._capture_replay_miss = MagicMock()

    def boom(*a, **k):
        raise ValueError("schema drift")

    monkeypatch.setattr("src.calendar_replay.parse_with_diagnostics", boom)
    for _ in range(checker._REPLAY_FAILURE_THRESHOLD):
        await checker._try_calendar_replay(sniper_mode=False)

    assert checker._replay_circuit_open is True
    checker._capture_replay_miss.assert_called()


def test_reset_replay_circuit_rearms_breaker():
    """reset_replay_circuit clears the counter, the open flag, AND the
    once-per-window capture budget — a calibration dump consumed during
    Wednesday normal polling must not starve Friday's release-night
    capture (sweep finding)."""
    from src.checker import AvailabilityChecker

    cfg = _make_config()
    cfg.use_calendar_replay = True
    c = AvailabilityChecker(cfg, MagicMock(), MagicMock())
    c._replay_failure_count = 5
    c._replay_circuit_open = True
    c._replay_capture_dumped_this_window = True

    c.reset_replay_circuit()
    assert c._replay_failure_count == 0
    assert c._replay_circuit_open is False
    assert c._replay_capture_dumped_this_window is False


def test_priority_capture_bypasses_window_budget():
    """The post-release DOM-beat-replay capture is the artifact the whole
    mechanism exists for (it root-caused 06/05) — it must dump even when
    a transient suspect-empty body consumed the window budget earlier
    (sweep finding); non-priority dumps still respect the budget."""
    from src.checker import AvailabilityChecker

    cfg = _make_config()
    cfg.use_calendar_replay = True
    c = AvailabilityChecker(cfg, MagicMock(), MagicMock())
    c._dump_body = MagicMock()
    c._last_replay_body = b"\x0a" * 30
    c._replay_capture_dumped_this_window = True

    c._capture_replay_miss()  # budget consumed → no dump
    c._dump_body.assert_not_called()

    c._capture_replay_miss(priority=True)  # release-night artifact → dump
    c._dump_body.assert_called_once()


def test_sniper_window_start_resets_replay_circuit(monkeypatch):
    """The monitor must re-arm the breaker when a sniper window STARTS —
    a circuit opened during Wednesday normal polling (schema drift) must
    not forfeit Friday's release-night fast path (merge-review finding:
    the only reset was at window END)."""
    from unittest.mock import patch as _patch
    from src.monitor import TockMonitor

    cfg = _make_config()
    browser = MagicMock()
    checker = MagicMock()
    notifier = MagicMock()
    tracker = MagicMock()
    with _patch("src.monitor.TockBooker"):
        monitor = TockMonitor(cfg, browser, checker, notifier, tracker)
    monitor._sniper_active = False
    monkeypatch.setattr(
        monitor, "_sniper_window_info", lambda now: "20:10"
    )

    interval = monitor._get_poll_interval()
    assert interval == 0
    checker.reset_replay_circuit.assert_called_once()


# --------------------------------------------------------------------------- #
# Fix #2 — replay-sourced slots reload the stale warm page before clicking
# --------------------------------------------------------------------------- #

def _make_booker():
    from src.booker import TockBooker
    return TockBooker(_make_config(), MagicMock(), MagicMock())


def _instrumented(
    booker, page, calls,
    click_results=(False,), day_results=(True,), container_ok=True,
):
    """Wire _book_single's collaborators to record an ordered call trace.

    click_results / day_results: successive returns (last repeats).
    container_ok: what the post-reload calendar_container wait reports.
    """
    clicks = list(click_results)
    days = list(day_results)

    async def fake_click(*a, **k):
        calls.append("click")
        return clicks.pop(0) if len(clicks) > 1 else clicks[0]

    async def fake_reload(*a, **k):
        calls.append("reload")

    async def fake_day_click(*a, **k):
        calls.append("day")
        return days.pop(0) if len(days) > 1 else days[0]

    async def fake_wait_for_selector(*a, **k):
        calls.append("container")
        return container_ok

    # The replay warm-page path now NAVIGATES (page.goto to the slot's own URL,
    # Fix 3 on the warm path) instead of page.reload(). Record either as the
    # warm-navigation step so the existing reload→container→click traces hold.
    page.reload = fake_reload
    page.goto = fake_reload
    booker._click_time_slot = fake_click
    booker._click_calendar_day = fake_day_click
    booker._wait_for_selector = fake_wait_for_selector
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()
    booker._booking_screenshot = AsyncMock()
    booker._wait_for_checkout = AsyncMock(return_value=False)
    # This tracer asserts the single-attempt reload→container→click→day order.
    # Pin one attempt so the Fix-4 click-retry loop doesn't multiply the trace;
    # retry behavior is covered in tests/test_slot_click_retry.py.
    booker.config.slot_click_max_tries = 1


def _warm_page():
    p = AsyncMock()
    p.is_closed = MagicMock(return_value=False)
    return p


def test_replay_slots_carry_source_marker():
    """The parser marks its slots so downstream consumers know no page
    has rendered them yet; DOM-found slots default to 'dom'."""
    from src.calendar_replay import parse_available_slots

    body = calendar_body(date_section("2026-05-15", ["17:00"]))
    slots = parse_available_slots(body, [TARGET])
    assert slots and all(s.source == "replay" for s in slots)
    assert AvailableSlot(TARGET, "5:00 PM", "Friday").source == "dom"


def test_source_not_part_of_slot_identity():
    """source (like target_selector) must not affect slot equality —
    monitor/booker dedupe compares detection identity only."""
    a = AvailableSlot(TARGET, "5:00 PM", "Friday", source="dom")
    b = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")
    assert a == b


@pytest.mark.asyncio
async def test_replay_slot_reload_container_then_strict_click():
    """The final post-reload sequence (two merge-review rounds): reload →
    wait for calendar_container (domcontentloaded fires before SPA
    hydration) → strict time click TRUSTING the page's own ?date= URL,
    with the day-click fallback ARMED. No unconditional day click: the
    booker's _click_calendar_day matches day-NUMBER text with no month
    validation, so clicking it eagerly is itself a wrong-date vector
    (and can de-select the date the URL already selected)."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls, day_results=(False,))
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert ok is False
    assert calls == ["reload", "container", "click", "day"], (
        f"replay slot: reload, container wait, strict click first, "
        f"day-click only as fallback; got {calls}"
    )


@pytest.mark.asyncio
async def test_replay_slot_fallback_day_click_recovers_slot():
    """Recovery path: ?date= didn't render buttons → strict click misses →
    fallback day click → retry click books the slot."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(
        booker, page, calls,
        click_results=(False, True), day_results=(True,),
    )
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert calls == ["reload", "container", "click", "day", "click"], (
        f"failed strict click must day-click and retry once; got {calls}"
    )


@pytest.mark.asyncio
async def test_replay_slot_no_unconditional_day_click_on_success():
    """When the strict click succeeds straight off the reloaded ?date=
    page, NO day click ever runs (month-boundary wrong-date protection)."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls, click_results=(True,))
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert calls == ["reload", "container", "click"], (
        f"successful strict click must not day-click at all; got {calls}"
    )


@pytest.mark.asyncio
async def test_dom_slot_does_not_reload_warm_page():
    """DOM-found slots were detected ON this page's current content —
    reloading would add latency, and the day-click fallback stays
    DISARMED (the checker already had the day selected)."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls)
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="dom")

    await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert calls == ["click"], f"dom slot must not reload or day-click; got {calls}"


@pytest.mark.asyncio
async def test_reload_failure_on_open_page_still_attempts_click():
    """A reload hiccup with the page still ALIVE must not abort — the
    container wait + day click + strict click still run."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls, day_results=(False,))

    async def failing_reload(*a, **k):
        calls.append("reload")
        raise RuntimeError("net hiccup")

    page.goto = failing_reload   # warm-replay path navigates via goto now
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert ok is False
    assert calls == ["reload", "container", "click", "day"], (
        f"click must still run after a failed reload; got {calls}"
    )


@pytest.mark.asyncio
async def test_reload_failure_on_dead_page_fails_fast():
    """If the page DIED during the reload (CF kill, renderer crash), fail
    fast instead of grinding the full click pipeline against a dead page
    (merge-review finding) — sibling race tasks / the next poll recover."""
    booker = _make_booker()
    page = _warm_page()
    # is_closed: warm-page checks at entry (×2) say open; post-reload says dead
    page.is_closed = MagicMock(side_effect=[False, False, True])
    calls: list[str] = []
    _instrumented(booker, page, calls)

    async def dying_reload(*a, **k):
        calls.append("reload")
        raise RuntimeError("Target closed")

    page.goto = dying_reload   # warm-replay path navigates via goto now
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert ok is False
    assert calls == ["reload"], (
        f"dead page after reload must fail fast; got {calls}"
    )


@pytest.mark.asyncio
async def test_replay_reload_aborts_when_container_never_renders():
    """If calendar_container never appears within the budget the page is
    hosed — return False instead of clicking into a blank SPA."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls, container_ok=False)
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    ok = await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert ok is False
    assert calls == ["reload", "container"], (
        f"missing calendar container must abort the attempt; got {calls}"
    )


@pytest.mark.asyncio
async def test_replay_slot_skips_reload_when_race_already_won():
    """A task that already lost the race must not pay the ~10s reload
    (adversarial-verify finding: no booking_won gate before the reload)."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls)
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")
    won = asyncio.Event()
    won.set()

    ok = await booker._book_single(slot, won, warm_page=page)
    assert ok is False
    assert calls == [], f"won race must abort before reload/click; got {calls}"
    booker.notifier.booking_aborted.assert_called()


@pytest.mark.asyncio
async def test_reload_block_does_not_duplicate_selector_waits():
    """The reload block must NOT run its own selector wait —
    _click_time_slot owns that wait; duplicating it stacked up to +4s on
    the 06/05-shaped DOM (only the generic selector matches)."""
    booker = _make_booker()
    page = _warm_page()
    calls: list[str] = []
    _instrumented(booker, page, calls)
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday", source="replay")

    await booker._book_single(slot, asyncio.Event(), warm_page=page)
    assert page.wait_for_selector.await_count == 0, (
        "reload block must not wait on selectors itself "
        f"(awaited {page.wait_for_selector.await_count}×)"
    )


# --------------------------------------------------------------------------- #
# Fix #1 completions (adversarial-verify minors)
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Gap 3 — selector waits race concurrently instead of 2s×2 sequentially
# --------------------------------------------------------------------------- #

class _SelectiveWaitPage:
    """Page stub whose wait_for_selector succeeds only for `matching`
    selectors and raises (like a Playwright timeout) for the rest."""

    def __init__(self, matching: set[str]):
        self.matching = matching
        self.waited: list[str] = []

    def is_closed(self):
        return False

    async def wait_for_selector(self, selector, timeout=None):
        self.waited.append(selector)
        if selector in self.matching:
            return MagicMock()
        await asyncio.sleep(0)  # let competing waits run
        raise TimeoutError(f"no {selector}")


@pytest.mark.asyncio
async def test_wait_any_slot_button_races_all_selectors():
    """The 06/05 DOM matched ONLY the generic Book selector (3rd in the
    cascade). The old sequential wait burned 2s×2 on the first two before
    ever looking further; the concurrent race must cover ALL selectors
    and report success."""
    from src.selectors import get_slot_button_selectors

    booker = _make_booker()
    generic = 'button:visible:has-text("Book")'
    assert generic in get_slot_button_selectors()[2:], (
        "precondition: the generic selector is NOT in the first two"
    )
    page = _SelectiveWaitPage(matching={generic})

    found = await booker._wait_any_slot_button(page, timeout_ms=500)
    assert found is True
    assert set(page.waited) == set(get_slot_button_selectors()), (
        "every selector in the cascade must be raced concurrently"
    )


@pytest.mark.asyncio
async def test_wait_any_slot_button_returns_false_when_none_match():
    booker = _make_booker()
    page = _SelectiveWaitPage(matching=set())

    found = await booker._wait_any_slot_button(page, timeout_ms=200)
    assert found is False


@pytest.mark.asyncio
async def test_click_time_slot_uses_concurrent_wait():
    """_click_time_slot must use the concurrent race, not the old
    sequential [:2] loop."""
    booker = _make_booker()
    booker._wait_any_slot_button = AsyncMock(return_value=False)
    page = MagicMock()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.locator = MagicMock(return_value=loc)
    slot = AvailableSlot(TARGET, "5:00 PM", "Friday")

    ok = await booker._click_time_slot(page, slot)
    assert ok is False
    booker._wait_any_slot_button.assert_awaited_once()


def test_wait_any_default_budget_matches_old_worst_case():
    """Merge-review finding: a 2s shared budget HALVED the old sequential
    worst case (2s × 2). Buttons rendering at t≈3s under release-night
    load were caught before and missed after. Default must be 4000ms —
    same worst case as before, instant when any selector matches sooner."""
    import inspect
    from src.booker import TockBooker

    sig = inspect.signature(TockBooker._wait_any_slot_button)
    assert sig.parameters["timeout_ms"].default == 4000


@pytest.mark.asyncio
async def test_wait_any_handles_success_and_instant_failures_same_batch():
    """One selector resolves while others raise in the SAME event-loop
    batch (e.g. page context tearing down): the race must still report
    True and retrieve every completed task's exception (merge-review
    finding: any() short-circuit left exceptions un-retrieved →
    'Task exception was never retrieved' spam in bot.log mid-race)."""
    from src.selectors import get_slot_button_selectors

    booker = _make_booker()

    class _InstantPage:
        def __init__(self):
            self.matching = {get_slot_button_selectors()[2]}

        async def wait_for_selector(self, selector, timeout=None):
            # No awaits at all: every task completes on its first step,
            # so successes and failures land in one done-batch.
            if selector in self.matching:
                return MagicMock()
            raise TimeoutError(f"no {selector}")

    found = await booker._wait_any_slot_button(_InstantPage(), timeout_ms=200)
    assert found is True


# --------------------------------------------------------------------------- #
# Fix #4 — oversized bodies are rejected before the synchronous decode
# --------------------------------------------------------------------------- #

def test_body_looks_protobuf_rejects_oversized_body():
    from src.calendar_replay import (
        _PROTOBUF_MAX_PLAUSIBLE_BYTES,
        body_looks_protobuf,
    )

    assert body_looks_protobuf(b"\x0a" * (_PROTOBUF_MAX_PLAUSIBLE_BYTES + 1)) is False


def test_body_looks_protobuf_accepts_benu_scale_body():
    """Real high-volume bodies are tens of KB — far below the cap."""
    from src.calendar_replay import body_looks_protobuf

    assert body_looks_protobuf(b"\x0a" * 120_000) is True


# --------------------------------------------------------------------------- #
# Hardenings — varint input guard, non-bytes body coercion
# --------------------------------------------------------------------------- #

def test_proto_fixture_varint_rejects_negative():
    with pytest.raises(ValueError):
        varint(-1)


def test_parse_accepts_bytearray_body():
    from src.calendar_replay import parse_available_slots

    body = bytearray(calendar_body(date_section("2026-05-15", ["17:00"])))
    slots = parse_available_slots(body, [TARGET])
    assert [(s.slot_date_str, s.slot_time) for s in slots] == [
        ("2026-05-15", "5:00 PM")
    ]
