"""Confirm-path hardening (06/26 23:30 incident).

At 23:30 a resurfaced slot reached /checkout/confirm-purchase. 'Checkout
detected via url' fired at SPA route-commit BEFORE the content rendered; the
one-shot non-waiting CVC/payment probes all missed; confirm was clicked 1s
later with the CVC provably unfilled; verification failed; and
booking_uncertain.json was written with NO artifact — the outcome is
permanently unknowable.

Four hardening measures under test:
  (a) render-settle wait after checkout detection, before payment probes
      (bounded 4s, non-fatal on timeout — never abort a won hold over this)
  (b) waiting CVC fill — retry the frame scan ~3s (12 x 250ms) and log the
      MISS at WARNING (the 23:30 miss logged at DEBUG = undiagnosable)
  (c) confirm-stage artifact — dump faildom_confirm_*.html (+ screenshot)
      BEFORE booking_uncertain.json is written; capture never raises
  (d) text-based confirmation detection — CONSUMER_CHECKOUT_DIALOG_MIGRATION
      can render success in-place with NO URL change; scan visible text for
      strong markers as an additive OR-signal after selector/URL checks fail
"""
import asyncio
import logging
import os
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.checker import AvailableSlot


def _make_booker(card_cvc: str = "123", debug_screenshots: bool = False):
    from src.booker import TockBooker
    from src.config import Config
    config = Config(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="test",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=debug_screenshots, discord_webhook_url="",
        card_cvc=card_cvc,
    )
    return TockBooker(config, MagicMock(), MagicMock())


def _make_slot():
    return AvailableSlot(
        slot_date=date(2026, 7, 3), slot_time="8:00 PM", day_of_week="Friday",
    )


def _timeout_exc():
    return Exception("timeout — test")


# ---------------------------------------------------------------------------
# (a) render-settle wait
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_settle_returns_true_when_confirm_and_payment_render(caplog):
    """Both signals (confirm button visible + a payment-ish element) resolve
    → settle succeeds, no WARNING."""
    import src.selectors as sel
    from src.booker import _CHECKOUT_RENDER_PAYMENT_SELECTOR
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()

    confirm_selector = sel.get("confirm_button")

    async def sel_side_effect(selector, **kw):
        if selector == confirm_selector:
            return MagicMock()
        if selector == _CHECKOUT_RENDER_PAYMENT_SELECTOR:
            return MagicMock()
        raise _timeout_exc()

    page.wait_for_selector = AsyncMock(side_effect=sel_side_effect)

    with caplog.at_level(logging.DEBUG, logger="src.booker"):
        result = await booker._settle_checkout_render(page, slot)

    assert result is True
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert not warnings, f"No WARNING expected on settled render: {warnings}"


@pytest.mark.asyncio
async def test_settle_timeout_logs_warning_and_returns_false(caplog):
    """Payment signal never renders → WARNING logged, returns False —
    but never raises (non-fatal by contract)."""
    import src.selectors as sel
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()

    confirm_selector = sel.get("confirm_button")

    async def sel_side_effect(selector, **kw):
        if selector == confirm_selector:
            return MagicMock()
        raise _timeout_exc()  # payment signal never appears

    page.wait_for_selector = AsyncMock(side_effect=sel_side_effect)

    with caplog.at_level(logging.DEBUG, logger="src.booker"):
        result = await booker._settle_checkout_render(page, slot)

    assert result is False
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "render" in r.getMessage()
    ]
    assert warnings, "Expected a WARNING mentioning the render settle timeout"


@pytest.mark.asyncio
async def test_prepare_runs_settle_before_payment_probes():
    """_prepare_for_confirm must run the render-settle wait BEFORE the
    payment probes (the 23:30 probes ran against a half-rendered page)."""
    booker = _make_booker(card_cvc="")
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/abc"

    order = []

    async def fake_settle(*a, **k):
        order.append("settle")
        return True

    async def fake_needs_payment(*a, **k):
        order.append("probe")
        return False

    with patch.object(booker, "_settle_checkout_render",
                      side_effect=fake_settle), \
         patch.object(booker, "_page_needs_payment",
                      side_effect=fake_needs_payment), \
         patch.object(booker, "_has_saved_card",
                      AsyncMock(return_value=False)):
        result = await booker._prepare_for_confirm(page, slot)

    assert result is True
    assert order[:2] == ["settle", "probe"], (
        f"render-settle must run before payment probes; got order={order}"
    )


@pytest.mark.asyncio
async def test_prepare_proceeds_when_settle_times_out():
    """A settle timeout is NON-FATAL: _prepare_for_confirm proceeds
    best-effort and can still return True (never abort a won hold)."""
    booker = _make_booker(card_cvc="")
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/abc"

    with patch.object(booker, "_settle_checkout_render",
                      AsyncMock(return_value=False)) as settle, \
         patch.object(booker, "_page_needs_payment",
                      AsyncMock(return_value=False)), \
         patch.object(booker, "_has_saved_card",
                      AsyncMock(return_value=False)):
        result = await booker._prepare_for_confirm(page, slot)

    assert result is True, "settle timeout must not abort the confirm prep"
    settle.assert_awaited_once()


# ---------------------------------------------------------------------------
# (b) waiting CVC fill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_cvc_fill_retries_until_found_and_logs_info(caplog):
    """The frame scan retries; a hit on the 3rd scan fills and logs the
    EXISTING '[book] CVC filled.' string at INFO (forensics greps rely on it)."""
    booker = _make_booker(card_cvc="123")
    page = AsyncMock()
    el = AsyncMock()
    booker.browser.find_in_frames = AsyncMock(side_effect=[None, None, el])

    with patch("src.booker._CVC_FILL_RETRY_INTERVAL_SEC", 0.0), \
         caplog.at_level(logging.INFO, logger="src.booker"):
        await booker._fill_cvc(page)

    assert booker.browser.find_in_frames.await_count == 3, (
        f"expected 3 frame scans (hit on 3rd); got "
        f"{booker.browser.find_in_frames.await_count}"
    )
    el.fill.assert_awaited_once_with("123")
    infos = [
        r for r in caplog.records
        if r.levelno == logging.INFO and "[book] CVC filled." in r.getMessage()
    ]
    assert infos, "success must log the existing '[book] CVC filled.' at INFO"


@pytest.mark.asyncio
async def test_cvc_fill_first_scan_hit_does_not_retry():
    """Happy path: CVC present on the first scan → exactly one scan."""
    booker = _make_booker(card_cvc="123")
    page = AsyncMock()
    el = AsyncMock()
    booker.browser.find_in_frames = AsyncMock(return_value=el)

    with patch("src.booker._CVC_FILL_RETRY_INTERVAL_SEC", 0.0):
        await booker._fill_cvc(page)

    assert booker.browser.find_in_frames.await_count == 1
    el.fill.assert_awaited_once_with("123")


@pytest.mark.asyncio
async def test_cvc_miss_after_budget_logs_warning_and_continues(caplog):
    """All 12 scans miss → WARNING (NOT debug — the 23:30 failure was only
    undiagnosable because the miss logged at DEBUG) and no exception."""
    booker = _make_booker(card_cvc="123")
    page = AsyncMock()
    booker.browser.find_in_frames = AsyncMock(return_value=None)

    with patch("src.booker._CVC_FILL_RETRY_INTERVAL_SEC", 0.0), \
         caplog.at_level(logging.DEBUG, logger="src.booker"):
        await booker._fill_cvc(page)  # must not raise

    assert booker.browser.find_in_frames.await_count == 12, (
        f"expected 12 frame scans before giving up; got "
        f"{booker.browser.find_in_frames.await_count}"
    )
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "CVC field not found after" in r.getMessage()
        and "proceeding to confirm without it" in r.getMessage()
    ]
    assert warnings, (
        "CVC miss must log at WARNING with the budget and 'proceeding to "
        f"confirm without it'; records: {[r.getMessage() for r in caplog.records]}"
    )


# ---------------------------------------------------------------------------
# (c) confirm-stage artifact
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_verification_failure_captures_artifact_before_uncertain_write():
    """_book_single's soft-win path must capture the confirm-stage artifacts
    BEFORE booking_uncertain.json is written (assert strict ordering)."""
    booker = _make_booker()
    slot = _make_slot()
    booking_won = asyncio.Event()

    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.url = "https://www.exploretock.com/test/checkout/confirm-purchase"
    booker.browser = MagicMock()
    booker.browser.new_page = AsyncMock(return_value=page)

    order = []

    async def fake_capture(*a, **k):
        order.append("artifact")

    def fake_write_uncertain(*a, **k):
        order.append("uncertain")

    with patch.object(booker, "_wait_for_selector",
                      AsyncMock(return_value=True)), \
         patch.object(booker, "_click_calendar_day",
                      AsyncMock(return_value=True)), \
         patch.object(booker, "_click_time_slot",
                      AsyncMock(return_value=True)), \
         patch.object(booker, "_wait_for_checkout",
                      AsyncMock(return_value=True)), \
         patch.object(booker, "_booking_screenshot", AsyncMock()), \
         patch.object(booker, "_prepare_for_confirm",
                      AsyncMock(return_value=True)), \
         patch.object(booker, "_execute_confirm_click_and_verify",
                      AsyncMock(return_value=False)), \
         patch.object(booker, "_capture_confirm_failure_artifacts",
                      side_effect=fake_capture), \
         patch("src.booking_uncertain.write_uncertain",
               side_effect=fake_write_uncertain):
        result = await booker._book_single(slot, booking_won)

    assert result is False
    assert order == ["artifact", "uncertain"], (
        f"artifact capture must precede booking_uncertain.json write; "
        f"got {order}"
    )
    assert booker._unverified_confirm_slot is slot


@pytest.mark.asyncio
async def test_capture_writes_faildom_confirm_dump_and_screenshot(tmp_path):
    """The artifact helper writes booking_failures/faildom_confirm_<ts>_<date>.html
    via the existing failure-dump pattern, plus a screenshot alongside."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/confirm-purchase"
    page.content = AsyncMock(return_value="<html>half-rendered checkout</html>")
    page.screenshot = AsyncMock()

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)):
        await booker._capture_confirm_failure_artifacts(page, slot)

    dumps = [
        f for f in os.listdir(tmp_path)
        if f.startswith("faildom_confirm_") and f.endswith("_2026-07-03.html")
    ]
    assert dumps, (
        f"expected a faildom_confirm_*_2026-07-03.html dump in {tmp_path}; "
        f"found {os.listdir(tmp_path)}"
    )
    content = (tmp_path / dumps[0]).read_text()
    assert "half-rendered checkout" in content

    page.screenshot.assert_awaited()
    shot_kwargs = page.screenshot.await_args.kwargs
    assert "failshot_confirm_" in shot_kwargs.get("path", ""), (
        f"screenshot must be written alongside the dump; kwargs={shot_kwargs}"
    )


@pytest.mark.asyncio
async def test_capture_swallows_all_artifact_exceptions(tmp_path):
    """Artifact capture must NEVER raise past the verification path — even
    when both the DOM dump and the screenshot blow up."""
    booker = _make_booker()
    slot = _make_slot()
    page = AsyncMock()
    page.url = "https://www.exploretock.com/test/checkout/confirm-purchase"
    page.screenshot = AsyncMock(side_effect=RuntimeError("shot boom"))

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)), \
         patch.object(booker, "_dump_click_failure",
                      AsyncMock(side_effect=RuntimeError("dump boom"))):
        # Must not raise
        await booker._capture_confirm_failure_artifacts(page, slot)


# ---------------------------------------------------------------------------
# (d) text-based confirmation detection
# ---------------------------------------------------------------------------

def _confirm_page(url: str = "https://www.exploretock.com/test/checkout/confirm-purchase"):
    """Page mock where the booking_confirmed selector wait times out and the
    URL carries none of the confirmation substrings."""
    page = AsyncMock()
    page.url = url
    page.click = AsyncMock()
    page.wait_for_selector = AsyncMock(side_effect=_timeout_exc())
    return page


@pytest.mark.asyncio
async def test_text_marker_converts_selector_timeout_to_success(caplog):
    """Selector wait times out, URL never changes (dialog-migration checkout),
    but the page's visible text carries a strong confirmation marker →
    verified success, and the matching marker is logged."""
    booker = _make_booker()
    slot = _make_slot()
    page = _confirm_page()
    page.evaluate = AsyncMock(return_value="Confirmation number")

    with patch("src.booker._CONFIRM_URL_RECHECK_DELAY_SEC", 0), \
         caplog.at_level(logging.INFO, logger="src.booker"):
        result = await booker._execute_confirm_click_and_verify(page, slot)

    assert result is True, (
        "a confirmation-text marker hit must convert the selector timeout "
        "into a verified success"
    )
    assert any(
        "Confirmation number" in r.getMessage() for r in caplog.records
    ), "the matching marker must be logged"


@pytest.mark.asyncio
async def test_no_marker_keeps_failure_path(caplog):
    """No marker in the visible text → the existing failure path is kept
    (returns False, SELECTOR_FAILED logged) — the scan is an additive
    OR-signal, never a rescue-all."""
    booker = _make_booker()
    slot = _make_slot()
    page = _confirm_page()
    page.evaluate = AsyncMock(return_value=None)

    with patch("src.booker._CONFIRM_URL_RECHECK_DELAY_SEC", 0), \
         caplog.at_level(logging.DEBUG, logger="src.booker"):
        result = await booker._execute_confirm_click_and_verify(page, slot)

    assert result is False
    assert page.evaluate.await_count >= 1, (
        "the text scan must actually run before declaring failure"
    )
    assert any(
        "SELECTOR_FAILED" in r.getMessage() for r in caplog.records
    ), "the existing SELECTOR_FAILED diagnostics must be preserved"


@pytest.mark.asyncio
async def test_marker_scan_error_keeps_failure_path():
    """A page.evaluate error (page closed / mid-navigation) must not raise
    and must not fake a success."""
    booker = _make_booker()
    slot = _make_slot()
    page = _confirm_page()
    page.evaluate = AsyncMock(side_effect=RuntimeError("page closed"))

    with patch("src.booker._CONFIRM_URL_RECHECK_DELAY_SEC", 0):
        result = await booker._execute_confirm_click_and_verify(page, slot)

    assert result is False


@pytest.mark.asyncio
async def test_marker_scan_nonstring_result_is_not_success():
    """Defensive: an AsyncMock-shaped / non-string evaluate result must not
    be treated as a marker hit (same trap as classify_booking_terminal_state)."""
    booker = _make_booker()
    slot = _make_slot()
    page = _confirm_page()
    page.evaluate = AsyncMock(return_value=MagicMock())

    with patch("src.booker._CONFIRM_URL_RECHECK_DELAY_SEC", 0):
        result = await booker._execute_confirm_click_and_verify(page, slot)

    assert result is False


def test_confirmation_markers_defined_and_include_known_strings():
    """The markers live in selectors.py (additive confirmation markers) and
    include the strings from the spec."""
    from src.selectors import CONFIRMATION_TEXT_MARKERS
    assert CONFIRMATION_TEXT_MARKERS, "markers must be non-empty"
    lowered = [m.lower() for m in CONFIRMATION_TEXT_MARKERS]
    for expected in (
        "reservation is confirmed",
        "confirmation number",
    ):
        assert expected in lowered, (
            f"expected marker {expected!r} in CONFIRMATION_TEXT_MARKERS"
        )


@pytest.mark.asyncio
async def test_existing_url_success_path_untouched():
    """Existing success path preserved: URL containing 'confirmation' still
    verifies WITHOUT any text scan (page.evaluate never called)."""
    booker = _make_booker()
    slot = _make_slot()
    page = _confirm_page(
        url="https://www.exploretock.com/test/checkout/confirmation"
    )
    page.evaluate = AsyncMock(return_value=None)

    result = await booker._execute_confirm_click_and_verify(page, slot)

    assert result is True
    page.evaluate.assert_not_awaited()


# ---------------------------------------------------------------------------
# Pre-deploy review fixes (07/02): artifact visibility, confirm-dump cap,
# CVC fill resilience, marker hygiene
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_confirm_dump_bypasses_shared_faildom_cap(tmp_path):
    """P1: a race-loss burst can fill the shared 50-file faildom_ cap with
    click/checkout dumps; the confirm-stage artifact (the one fix (c) exists
    to guarantee) must still be written — it counts only against its own cap."""
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html>confirm page</html>")
    page.url = "https://www.exploretock.com/test/checkout/confirm-purchase"
    for i in range(50):
        (tmp_path / f"faildom_click_2026_{i}.html").write_text("x")

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)):
        await booker._dump_click_failure(page, _make_slot(), stage="confirm")

    dumps = [f for f in os.listdir(tmp_path) if f.startswith("faildom_confirm_")]
    assert dumps, (
        "confirm-stage dump must be written despite 50 faildom_click_ files"
    )


@pytest.mark.asyncio
async def test_confirm_dump_own_cap_skip_logs_warning(tmp_path, caplog):
    """P1: when the confirm-dump cap itself is hit, the skip must be visible
    at production INFO level (WARNING+), not DEBUG."""
    from src.booker import _MAX_CONFIRM_DUMPS
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html/>")
    page.url = "https://x/checkout"
    for i in range(_MAX_CONFIRM_DUMPS):
        (tmp_path / f"faildom_confirm_2026_{i}.html").write_text("x")

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)), \
         caplog.at_level(logging.DEBUG, logger="src.booker"):
        await booker._dump_click_failure(page, _make_slot(), stage="confirm")

    dumps = [f for f in os.listdir(tmp_path) if f.startswith("faildom_confirm_")]
    assert len(dumps) == _MAX_CONFIRM_DUMPS, "confirm-dump cap must hold"
    warnings = [
        r for r in caplog.records
        if r.levelno >= logging.WARNING and "cap" in r.getMessage()
    ]
    assert warnings, "confirm-dump cap skip must log at WARNING+"


@pytest.mark.asyncio
async def test_click_stage_cap_ignores_confirm_dumps(tmp_path):
    """Separation works both ways: confirm artifacts must not consume the
    click/checkout dump budget."""
    from src.booker import _MAX_CONFIRM_DUMPS
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(return_value="<html/>")
    page.url = "https://x/search"
    for i in range(_MAX_CONFIRM_DUMPS):
        (tmp_path / f"faildom_confirm_2026_{i}.html").write_text("x")

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)):
        await booker._dump_click_failure(page, _make_slot(), stage="click")

    click_dumps = [
        f for f in os.listdir(tmp_path) if f.startswith("faildom_click_2026-")
        or (f.startswith("faildom_click_") and f.endswith(".html")
            and "2026_" not in f)
    ]
    assert click_dumps, "click dump must be written despite confirm dumps present"


@pytest.mark.asyncio
async def test_capture_artifact_failures_log_warning(tmp_path, caplog):
    """P1: if the confirm-stage artifact capture ITSELF fails (page closed,
    screenshot error), that failure must be visible at production INFO level —
    otherwise the 06/26 'outcome permanently unknowable, and we don't even
    know the capture failed' gap silently reopens."""
    booker = _make_booker()
    page = AsyncMock()
    page.content = AsyncMock(side_effect=Exception("page closed"))
    page.screenshot = AsyncMock(side_effect=Exception("shot failed"))

    with patch("src.booker._BOOKING_FAILURE_DIR", str(tmp_path)), \
         caplog.at_level(logging.DEBUG, logger="src.booker"):
        await booker._capture_confirm_failure_artifacts(page, _make_slot())

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) >= 2, (
        "both the DOM-dump failure and the screenshot failure must log at "
        f"WARNING+; got: {[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_cvc_fill_exception_warns_and_keeps_retrying(caplog):
    """P2: el.fill() raising (element detached mid-render in the Stripe
    iframe) must not kill the WINNING task via the generic error handler —
    WARN and keep retrying within the remaining budget."""
    booker = _make_booker(card_cvc="123")
    page = AsyncMock()
    flaky = AsyncMock()
    flaky.fill = AsyncMock(side_effect=Exception("Element is not attached"))
    good = AsyncMock()
    booker.browser.find_in_frames = AsyncMock(side_effect=[flaky, good])

    with patch("src.booker._CVC_FILL_RETRY_INTERVAL_SEC", 0.0), \
         caplog.at_level(logging.DEBUG, logger="src.booker"):
        await booker._fill_cvc(page)  # must not raise

    good.fill.assert_awaited_once_with("123")
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "CVC fill attempt" in r.getMessage()
    ]
    assert warnings, "failed fill attempt must log a CVC-specific WARNING"
    infos = [r for r in caplog.records if "[book] CVC filled." in r.getMessage()]
    assert infos, "the retry after the failed fill must succeed and log INFO"


@pytest.mark.asyncio
async def test_marker_scan_error_logs_warning(caplog):
    """P2: 'text scan crashed' must be distinguishable from 'text scan ran and
    found no marker' at production INFO level."""
    booker = _make_booker()
    page = AsyncMock()
    page.evaluate = AsyncMock(side_effect=Exception("page closed"))

    with caplog.at_level(logging.DEBUG, logger="src.booker"):
        marker = await booker._detect_confirmation_text(page)

    assert marker is None
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING
        and "confirmation text scan" in r.getMessage()
    ]
    assert warnings, "marker-scan errors must log at WARNING"


def test_weak_marker_removed_from_confirmation_markers():
    """P2: a marker false-positive upgrades FAILED→CONFIRMED, which skips the
    booking_uncertain guard AND the red 'verify manually' alert. "you're going
    to" is generic enough to appear on non-confirmation pages — dropped."""
    from src.selectors import CONFIRMATION_TEXT_MARKERS
    lowered = [m.lower().replace("’", "'") for m in CONFIRMATION_TEXT_MARKERS]
    assert "you're going to" not in lowered, (
        "weak marker must be removed (false positive risk on drop night)"
    )
    # The three strong markers stay.
    for expected in ("reservation is confirmed", "you're all set",
                     "confirmation number"):
        assert expected in lowered
