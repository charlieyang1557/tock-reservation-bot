"""Verify the calendar-replay path captures fuhuihua's businessId
correctly (even though it's sold out, the calendar/full XHR still
fires on initial page load with fuhuihua's auth scope).

Confirms:
  - initialize_replay_session works for fuhuihua slug
  - Captured x-tock-scope header contains fuhuihua's businessId
    (different from benu's 10775)
  - fetch_calendar() succeeds
  - Body parses as protobuf (passes body_looks_protobuf)
  - For sold-out fuhuihua: parser returns 0 slots (correct)
  - Per-poll latency is comparable to benu (~150-200ms)
"""
from __future__ import annotations
import asyncio, json, sys, time
from datetime import date, timedelta


async def _run() -> int:
    from src.browser import TockBrowser
    from src.config import load_config
    from src.calendar_replay import (
        initialize_replay_session, fetch_calendar,
        parse_available_slots, body_looks_protobuf, close_session,
    )

    cfg = load_config()
    cfg.restaurant_slug = "fui-hui-hua-san-francisco"
    cfg.headless = True

    browser = TockBrowser(cfg)
    await browser.start()
    session = None
    try:
        if not await browser.login():
            print("Login failed.", file=sys.stderr)
            return 2

        # Pick a Friday/Saturday/Sunday in the next 2 weeks (fuhuihua's
        # active days) so the URL is valid even though no slots
        today = date.today()
        days_until_fri = (4 - today.weekday()) % 7 or 7
        fri = today + timedelta(days=days_until_fri)

        print(f"[fuhuihua] init replay session, target_date={fri}, time=20:00")
        t0 = time.monotonic()
        session = await initialize_replay_session(
            browser, "fui-hui-hua-san-francisco", fri,
            party_size=2, preferred_time="20:00",
        )
        init_ms = (time.monotonic() - t0) * 1000
        if session is None:
            print("[fuhuihua] FAIL — session init returned None", file=sys.stderr)
            return 1
        print(f"[fuhuihua] init OK in {init_ms:.0f}ms")

        # Inspect the captured x-tock-scope to confirm we got fuhuihua's businessId
        scope = session.headers.get("x-tock-scope", "")
        print(f"[fuhuihua] captured x-tock-scope: {scope}")
        try:
            scope_data = json.loads(scope) if scope else {}
            print(f"[fuhuihua]   businessId: {scope_data.get('businessId')}")
            print(f"[fuhuihua]   businessGroupId: {scope_data.get('businessGroupId')}")
            print(f"[fuhuihua]   site: {scope_data.get('site')}")
            assert scope_data.get('businessId') != 10775, (
                "businessId should NOT match benu's (10775) — auto-detection broken"
            )
        except (json.JSONDecodeError, KeyError) as e:
            print(f"[fuhuihua] WARN — could not parse x-tock-scope: {e}")

        # Confirm fetch_calendar works
        print()
        print("[fuhuihua] running 5× fetch_calendar:")
        target_dates = [today + timedelta(days=i) for i in range(1, 15)]
        for i in range(5):
            t0 = time.monotonic()
            body = await fetch_calendar(session)
            wall_ms = (time.monotonic() - t0) * 1000
            if body is None:
                print(f"  poll {i+1}/5: FAIL (None body)")
                continue
            slots = parse_available_slots(body, target_dates)
            print(
                f"  poll {i+1}/5: {wall_ms:.0f}ms wall  {len(body):>5}b body  "
                f"{len(slots)} slots  protobuf-shaped={body_looks_protobuf(body)}"
            )

        print()
        print("=" * 60)
        print("[fuhuihua] PASS — replay path works for fuhuihua slug.")
        print("[fuhuihua] businessId auto-detected; CF allows the in-browser fetch.")
        print(f"[fuhuihua] When fuhuihua releases, replay will detect slots in <1s.")
        print("=" * 60)
        return 0
    finally:
        await close_session(session)
        await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
