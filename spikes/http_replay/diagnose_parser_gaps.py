"""Diagnose: the protobuf calendar body has 18 KB of data per
restaurant, but the parser only extracts 3-5 unique times per date
when Tock's UI shows 17+ for the same date. Where do the extras go?

Hypothesis 1: parser regex doesn't match interpolated 15-min times
Hypothesis 2: protobuf encodes only "anchor" seating times; UI
              interpolates the in-between
Hypothesis 3: parser dedupes too aggressively and 5:30 PM appears
              once in the body so 5:45/5:15 never make it in

This script fetches the live calendar body and dumps every
time-shaped substring per date section, with byte offsets.
"""
from __future__ import annotations
import asyncio, sys, re
from datetime import date, timedelta


async def _run() -> int:
    from src.browser import TockBrowser
    from src.config import load_config
    from src.calendar_replay import (
        initialize_replay_session, fetch_calendar, close_session,
        _DATE_RE, _TIME_RE,
    )

    cfg = load_config()
    cfg.restaurant_slug = "benu"
    cfg.headless = True

    browser = TockBrowser(cfg)
    await browser.start()
    sess = None
    try:
        if not await browser.login():
            return 2
        target = date(2026, 5, 15)
        sess = await initialize_replay_session(
            browser, "benu", target, party_size=2, preferred_time="17:00",
        )
        if sess is None:
            print("Init failed.", file=sys.stderr)
            return 1
        body = await fetch_calendar(sess)
        if body is None:
            print("Fetch failed.", file=sys.stderr)
            return 1

        print(f"Body length: {len(body)} bytes")
        print()

        # 1. Every date marker
        date_hits = list(_DATE_RE.finditer(body))
        print(f"=== Date markers: {len(date_hits)} ===")
        seen = []
        first_off = {}
        for m in date_hits:
            d = m.group(0).decode()
            if d not in first_off:
                first_off[d] = m.start()
                seen.append(d)
        print(f"Unique dates: {seen}")
        print()

        # 2. For each date section, dump ALL time-shaped substrings
        for i, d in enumerate(seen):
            start = first_off[d]
            end = first_off[seen[i+1]] if i+1 < len(seen) else len(body)
            section = body[start:end]
            # Match ALL HH:MM patterns (no upper-bound filter)
            all_times = re.findall(rb"\b(\d{1,2}):(\d{2})\b", section)
            unique_times = sorted(set(
                f"{int(h):02d}:{int(m):02d}" for h, m in all_times
            ))
            print(f"=== Date section {d} (offsets {start}-{end}, {end-start}b) ===")
            print(f"  All distinct HH:MM strings: {unique_times}")
            # Also try a broader pattern — any digit:digit
            broader = re.findall(rb"(\d+):(\d+)", section)
            unique_broader = sorted(set(
                f"{int(h)}:{int(m)}" for h, m in broader if int(h) < 100 and int(m) < 100
            ))
            extras = sorted(set(unique_broader) - set(unique_times))
            if extras:
                print(f"  Broader pattern matches additional: {extras[:20]}")
            print()
    finally:
        await close_session(sess)
        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
