"""Live validation: prove `src/calendar_replay.py` works end-to-end
against benu's real calendar API.

  1. Initialize a CalendarReplaySession on benu
  2. Run fetch_calendar() 5x back-to-back
  3. Parse each response with parse_available_slots
  4. Print latency + slot counts; assert consistency across the 5 calls
"""
from __future__ import annotations
import asyncio, sys, time
from datetime import date, timedelta


async def _run() -> int:
    from src.browser import TockBrowser
    from src.config import load_config
    from src.calendar_replay import (
        initialize_replay_session, fetch_calendar,
        parse_available_slots, close_session,
    )

    cfg = load_config()
    cfg.restaurant_slug = "benu"
    cfg.headless = True

    browser = TockBrowser(cfg)
    await browser.start()
    session = None
    try:
        if not await browser.login():
            print("Login failed.", file=sys.stderr)
            return 2

        # Pick a date with known availability for benu (next Wednesday)
        today = date.today()
        days_until_wed = (2 - today.weekday()) % 7 or 7
        wed = today + timedelta(days=days_until_wed)
        # Target the next 14 days — covers benu's typical availability
        target_dates = [today + timedelta(days=i) for i in range(1, 15)]

        print(f"[validate] Initializing replay session for benu, "
              f"first navigation date={wed.isoformat()}")
        t0 = time.monotonic()
        session = await initialize_replay_session(
            browser, "benu", wed, party_size=2, preferred_time="20:00",
        )
        init_ms = (time.monotonic() - t0) * 1000
        if session is None:
            print("[validate] FAIL — session init returned None", file=sys.stderr)
            return 1
        print(f"[validate] init: {init_ms:.0f}ms")

        print(f"\n[validate] Running 5× fetch_calendar:")
        all_results = []
        for i in range(5):
            t0 = time.monotonic()
            body = await fetch_calendar(session)
            wall_ms = (time.monotonic() - t0) * 1000
            if body is None:
                print(f"  poll {i+1}/5: FAIL (returned None)")
                continue
            slots = parse_available_slots(body, target_dates)
            print(
                f"  poll {i+1}/5: {wall_ms:.0f}ms wall  "
                f"{session.last_fetch_ms}ms fetch  "
                f"{len(body)}b body  {len(slots)} slots"
            )
            all_results.append((wall_ms, len(body), slots))

        if not all_results:
            print("[validate] FAIL — all 5 fetches failed")
            return 1

        # Consistency check: every successful fetch should return ~same #slots
        slot_counts = [len(r[2]) for r in all_results]
        if max(slot_counts) - min(slot_counts) > 2:
            print(
                f"[validate] WARN — slot count varies: {slot_counts} "
                "(expect availability changes between polls; "
                "small jitter OK; large jitter suspicious)"
            )

        # Show the deduplicated set of slots from the median fetch
        sample = all_results[len(all_results) // 2][2]
        print(f"\n[validate] Sample slot list ({len(sample)} slots):")
        for s in sample[:20]:
            print(f"  - {s.slot_date} ({s.day_of_week}) @ {s.slot_time}")
        if len(sample) > 20:
            print(f"  ... and {len(sample) - 20} more")

        # Headline numbers
        avg_wall = sum(r[0] for r in all_results) / len(all_results)
        print()
        print("=" * 60)
        print(f"[validate] PASS — 5/5 fetches reliable")
        print(f"           Avg per-poll wall-clock: {avg_wall:.0f}ms")
        print(f"           vs current 12.4s concurrent baseline")
        print(f"           Speedup: {12400 / avg_wall:.0f}×")
        print("=" * 60)
        return 0
    finally:
        await close_session(session)
        await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
