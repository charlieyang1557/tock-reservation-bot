"""Fix 4 (2026-06-13) — retry the Book-button click up to N times per attempt.

A single click on the slot's Book button can fail to register (SPA hiccup /
transient state) even when the button is live; a later click often goes
through. The booker must NOT give up after one click — it re-clicks (default
10 tries) and waits a short window for checkout each time, breaking as soon as
checkout loads. Aborts early if another slot already won or the page closed.
"""
import asyncio
from dataclasses import fields
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.checker import AvailableSlot


def _make_booker(**cfg_over):
    from src.booker import TockBooker
    from src.config import Config
    base = dict(
        tock_email="t@t.com", tock_password="pw", restaurant_slug="benu",
        party_size=2, preferred_days=["Friday"], fallback_days=[],
        preferred_time="17:00", scan_weeks=4, dry_run=False, headless=True,
        sniper_days=["Friday"], sniper_times=["19:59"], sniper_duration_min=11,
        sniper_interval_sec=3, release_window_days=["Monday"],
        release_window_start="09:00", release_window_end="11:00",
        debug_screenshots=False, discord_webhook_url="", card_cvc="",
    )
    base.update(cfg_over)
    return TockBooker(Config(**base), MagicMock(), MagicMock())


def _slot(slot_time="5:00 PM"):
    return AvailableSlot(
        slot_date=date(2026, 6, 19), slot_time=slot_time, day_of_week="Friday",
    )


def _page():
    page = AsyncMock()
    page.is_closed = MagicMock(return_value=False)
    page.evaluate = AsyncMock()
    page.url = "https://www.exploretock.com/benu/search"
    return page


# --- config ----------------------------------------------------------------

def test_slot_click_max_tries_defaults_to_10():
    from src.config import Config
    for f in fields(Config):
        if f.name == "slot_click_max_tries":
            assert f.default == 10
            return
    raise AssertionError("Config has no slot_click_max_tries field")


# --- _click_until_checkout retry behavior ----------------------------------

@pytest.mark.asyncio
async def test_succeeds_on_first_try_clicks_once():
    booker = _make_booker()
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=True)

    ok = await booker._click_until_checkout(
        _page(), _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is True
    assert booker._attempt_slot_click.await_count == 1


@pytest.mark.asyncio
async def test_reclicks_until_checkout_loads():
    """Checkout fails the first 3 clicks, succeeds on the 4th → 4 clicks."""
    booker = _make_booker(slot_click_max_tries=10)
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(side_effect=[False, False, False, True])

    ok = await booker._click_until_checkout(
        _page(), _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is True
    assert booker._attempt_slot_click.await_count == 4


@pytest.mark.asyncio
async def test_retries_at_least_10_times_before_giving_up():
    """The whole point: do NOT give up after one click. With the default of
    10, a checkout that never loads still gets 10 click attempts."""
    booker = _make_booker(slot_click_max_tries=10)
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()

    ok = await booker._click_until_checkout(
        _page(), _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is False
    assert booker._attempt_slot_click.await_count == 10
    booker._dump_click_failure.assert_awaited_once()


@pytest.mark.asyncio
async def test_aborts_immediately_when_booking_already_won():
    booker = _make_booker()
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    won = asyncio.Event()
    won.set()

    ok = await booker._click_until_checkout(
        _page(), _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=won,
    )

    assert ok is False
    booker._attempt_slot_click.assert_not_awaited()


@pytest.mark.asyncio
async def test_skips_reclick_when_already_on_checkout_url():
    """Code-review: once a prior click has navigated toward checkout, retries
    must NOT re-click the Book button (a double-submit risks a duplicate hold
    / bounced page). With the page URL already on /checkout/, only try 1 may
    click; tries 2+ just confirm checkout."""
    booker = _make_booker(slot_click_max_tries=3)
    page = _page()
    page.url = "https://www.exploretock.com/benu/checkout/abc"
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()

    await booker._click_until_checkout(
        page, _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    # try 1 clicks; tries 2-3 see the checkout URL and skip the re-click
    assert booker._attempt_slot_click.await_count == 1


@pytest.mark.asyncio
async def test_stops_when_page_closes_mid_retry():
    booker = _make_booker(slot_click_max_tries=10)
    page = _page()
    page.is_closed = MagicMock(side_effect=[False, True])  # closes before 2nd try
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()

    ok = await booker._click_until_checkout(
        page, _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is False
    assert booker._attempt_slot_click.await_count == 1  # didn't keep clicking a dead page


# --- final full-budget checkout wait (codex HIGH: don't abandon slow checkout) ---

@pytest.mark.asyncio
async def test_final_full_wait_catches_slow_checkout_after_retries():
    """The short per-try waits use timeout_sec=2. If they all miss but we DID
    click, a FINAL full-timeout (~30s) checkout wait must still catch a slow-
    loading checkout — so the click-retry budget doesn't shrink the checkout
    patience and abandon a won hold."""
    booker = _make_booker(slot_click_max_tries=2)
    page = _page()
    booker._attempt_slot_click = AsyncMock(return_value=(True, True))
    # 2 short per-try waits miss, the final full wait succeeds
    booker._wait_for_checkout = AsyncMock(side_effect=[False, False, True])
    booker._dump_click_failure = AsyncMock()

    ok = await booker._click_until_checkout(
        page, _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is True
    assert booker._wait_for_checkout.await_count == 3       # 2 per-try + 1 final
    booker._dump_click_failure.assert_not_awaited()
    # the final wait must NOT use the short per-try timeout (it's the full one)
    final_kwargs = booker._wait_for_checkout.await_args_list[-1].kwargs
    assert final_kwargs.get("timeout_sec") in (None, 30)


@pytest.mark.asyncio
async def test_skip_mode_fallback_aborts_when_booking_won():
    """codex MEDIUM regression: the skip-mode calendar-day fallback in
    _attempt_slot_click must NOT start a fresh day-click + re-click once
    another slot has won — that would place a redundant hold."""
    booker = _make_booker()
    booker._click_tagged_slot = AsyncMock(return_value=False)
    booker._click_time_slot = AsyncMock(return_value=False)
    booker._click_calendar_day = AsyncMock(return_value=True)
    won = asyncio.Event()
    won.set()

    clicked, day_clicked = await booker._attempt_slot_click(
        _page(), _slot(), using_warm_page=True, skip_click=True,
        day_clicked=False, allow_tagged=False, booking_won=won,
    )

    assert clicked is False
    booker._click_calendar_day.assert_not_awaited()


# --- stale-warm-page reload recovery (drain residual fix) ------------------

@pytest.mark.asyncio
async def test_warm_page_reloads_once_when_no_button_then_clicks():
    """Drain residual: a handed-over warm page that's actually a stale
    (pre-release) prewarm DOM has no Book button. On a WARM page, if the first
    attempt finds no button, the booker reloads ONCE to the slot's URL and the
    retry then finds + clicks it — instead of failing all 10 tries."""
    booker = _make_booker(slot_click_max_tries=4)
    page = _page()
    page.goto = AsyncMock()
    booker._wait_for_selector = AsyncMock(return_value=True)
    booker._build_search_url = MagicMock(
        return_value="https://www.exploretock.com/benu/search?date=2026-06-19&size=2&time=20:00"
    )
    # first attempt: no button; after the reload: found + clicked → checkout
    booker._attempt_slot_click = AsyncMock(side_effect=[(False, True), (True, True)])
    booker._wait_for_checkout = AsyncMock(return_value=True)

    ok = await booker._click_until_checkout(
        page, _slot("8:00 PM"), owns_page=False, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    assert ok is True
    page.goto.assert_awaited_once()                       # reloaded once
    assert booker._attempt_slot_click.await_count == 2


@pytest.mark.asyncio
async def test_recovery_rearms_day_click_fallback():
    """code-review CONFIRMED: after the stale-page reload, the day-click
    fallback must be RE-ARMED (skip_click=True, day_clicked=False) — the fresh
    DOM has no day selected, so carrying day_clicked=True would permanently
    disable the calendar-day re-click and the slot button may never be found."""
    booker = _make_booker(slot_click_max_tries=4)
    page = _page()
    page.goto = AsyncMock()
    booker._wait_for_selector = AsyncMock(return_value=True)
    booker._build_search_url = MagicMock(
        return_value="https://www.exploretock.com/benu/search?date=2026-06-19&size=2&time=17:00"
    )
    seen = []

    async def fake_attempt(page, slot, *, using_warm_page, skip_click,
                           day_clicked, allow_tagged, booking_won=None):
        seen.append({"skip_click": skip_click, "day_clicked": day_clicked})
        return (False, day_clicked)  # never clicks → recovery once, then exhausts

    booker._attempt_slot_click = fake_attempt
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()

    # Enter as a replay-warm slot whose day was already clicked pre-recovery.
    await booker._click_until_checkout(
        page, _slot(), owns_page=False, skip_click=True,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    page.goto.assert_awaited_once()                 # recovery reloaded once
    assert seen[0]["day_clicked"] is True           # pre-recovery state
    post = seen[1:]
    assert post, "expected at least one post-recovery attempt"
    assert all(c["day_clicked"] is False for c in post), (
        f"day_clicked not re-armed after recovery: {seen}"
    )
    assert all(c["skip_click"] is True for c in post)


@pytest.mark.asyncio
async def test_no_reload_recovery_on_owned_page():
    """Recovery is for HANDED-OVER warm pages only — a cold-nav (owns_page)
    page was just navigated fresh, so no reload-recovery should fire."""
    booker = _make_booker(slot_click_max_tries=3)
    page = _page()
    page.goto = AsyncMock()
    booker._attempt_slot_click = AsyncMock(return_value=(False, True))
    booker._wait_for_checkout = AsyncMock(return_value=False)
    booker._dump_click_failure = AsyncMock()

    await booker._click_until_checkout(
        page, _slot(), owns_page=True, skip_click=False,
        day_clicked=True, booking_won=asyncio.Event(),
    )

    page.goto.assert_not_awaited()


# --- _wait_for_checkout honors a per-try timeout ---------------------------

@pytest.mark.asyncio
async def test_wait_for_checkout_honors_timeout_sec():
    booker = _make_booker()
    captured = {}

    async def cap_selector(sel, timeout=None):
        captured["selector"] = timeout
        raise Exception("no checkout")

    async def cap_url(pred, timeout=None):
        captured["url"] = timeout
        raise Exception("no url")

    async def cap_fn(js, timeout=None):
        captured["fn"] = timeout
        raise Exception("no fn")

    page = _page()
    page.wait_for_selector = AsyncMock(side_effect=cap_selector)
    page.wait_for_url = AsyncMock(side_effect=cap_url)
    page.wait_for_function = AsyncMock(side_effect=cap_fn)

    ok = await booker._wait_for_checkout(page, _slot(), timeout_sec=3)

    assert ok is False
    assert captured == {"selector": 3000, "url": 3000, "fn": 3000}
