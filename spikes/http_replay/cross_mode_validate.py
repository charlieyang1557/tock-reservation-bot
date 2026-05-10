"""Cross-mode validation: do legacy-concurrent, legacy-sequential, and
calendar-replay all agree on which (date, time) slots are bookable?

Runs each mode 3× back-to-back on benu, tabulates the (date, time)
pairs each one returns, and prints a diff:
  - PASS: replay returns slots that are a SUPERSET of legacy
    (replay sees more, but everything legacy sees is also in replay)
  - SUSPICIOUS: replay reports a (date, time) that legacy never sees
    across multiple polls → potential ghost slot from parser bug
  - SUSPICIOUS: legacy sees a (date, time) that replay never reports
    → potential miss from parser

Usage:
  python -m spikes.http_replay.cross_mode_validate
"""
from __future__ import annotations

import asyncio
import json
import sys
import os
from collections import defaultdict
from pathlib import Path


async def _gather_legacy(restaurant_slug: str, concurrent: bool) -> set[tuple[str, str]]:
    """Run one legacy check_all (no replay) and return the (date, time) set."""
    # Force replay OFF for this run via env override
    os.environ["USE_CALENDAR_REPLAY"] = "false"
    from src.config import load_config
    from src.browser import TockBrowser
    from src.checker import AvailabilityChecker
    from src.tracker import SlotTracker

    cfg = load_config()
    cfg.restaurant_slug = restaurant_slug
    cfg.headless = True
    cfg.dry_run = True
    cfg.use_calendar_replay = False

    browser = TockBrowser(cfg)
    await browser.start()
    try:
        if not await browser.login():
            print("Login failed.", file=sys.stderr)
            return set()
        tracker = SlotTracker()
        checker = AvailabilityChecker(cfg, browser, tracker)
        slots = await checker.check_all(concurrent=concurrent)
        return {(s.slot_date.isoformat(), s.slot_time) for s in slots}
    finally:
        await browser.close()


async def _gather_replay(restaurant_slug: str) -> set[tuple[str, str]]:
    """Run one replay-mode check_all and return the (date, time) set."""
    os.environ["USE_CALENDAR_REPLAY"] = "true"
    from src.config import load_config
    from src.browser import TockBrowser
    from src.checker import AvailabilityChecker
    from src.tracker import SlotTracker

    cfg = load_config()
    cfg.restaurant_slug = restaurant_slug
    cfg.headless = True
    cfg.dry_run = True
    cfg.use_calendar_replay = True

    browser = TockBrowser(cfg)
    await browser.start()
    try:
        if not await browser.login():
            print("Login failed.", file=sys.stderr)
            return set()
        tracker = SlotTracker()
        checker = AvailabilityChecker(cfg, browser, tracker)
        slots = await checker.check_all(concurrent=True)
        return {(s.slot_date.isoformat(), s.slot_time) for s in slots}
    finally:
        await checker.close_replay_session()
        await browser.close()


def _tabulate(label: str, pairs: set[tuple[str, str]]) -> None:
    by_date = defaultdict(list)
    for d, t in pairs:
        by_date[d].append(t)
    print(f"\n=== {label}: {len(pairs)} slots across {len(by_date)} dates ===")
    for d in sorted(by_date.keys()):
        times = sorted(by_date[d], key=_time_key)
        print(f"  {d}: {len(times):>2} slots — {', '.join(times)}")


def _time_key(t: str) -> int:
    import re
    m = re.match(r"(\d+):(\d+) (AM|PM)", t)
    if not m:
        return 9999
    hh = int(m.group(1)) % 12
    if m.group(3) == "PM":
        hh += 12
    return hh * 60 + int(m.group(2))


def _diff(a_label: str, a: set, b_label: str, b: set) -> None:
    only_a = a - b
    only_b = b - a
    common = a & b
    print(f"\n=== Diff: {a_label} vs {b_label} ===")
    print(f"  Common (in both): {len(common)} slots")
    print(f"  Only in {a_label}: {len(only_a)} slots")
    if only_a:
        for d, t in sorted(only_a):
            print(f"    + {d} {t}")
    print(f"  Only in {b_label}: {len(only_b)} slots")
    if only_b:
        for d, t in sorted(only_b):
            print(f"    + {d} {t}")


async def _run() -> int:
    slug = "benu"
    print(f"Cross-mode validation on {slug}")
    print("=" * 60)

    # Run each mode 3× and union the results so we capture everything
    # any of them ever sees (handles transient races / Tock state changes).
    legacy_seq: set[tuple[str, str]] = set()
    legacy_conc: set[tuple[str, str]] = set()
    replay: set[tuple[str, str]] = set()

    print("\n[1/3] legacy concurrent (3 runs)…")
    for i in range(3):
        s = await _gather_legacy(slug, concurrent=True)
        legacy_conc |= s
        print(f"  run {i+1}: {len(s)} slots")

    print("\n[2/3] legacy sequential (3 runs)…")
    for i in range(3):
        s = await _gather_legacy(slug, concurrent=False)
        legacy_seq |= s
        print(f"  run {i+1}: {len(s)} slots")

    print("\n[3/3] replay (3 runs)…")
    for i in range(3):
        s = await _gather_replay(slug)
        replay |= s
        print(f"  run {i+1}: {len(s)} slots")

    # Tabulate
    _tabulate("legacy concurrent (union of 3 runs)", legacy_conc)
    _tabulate("legacy sequential (union of 3 runs)", legacy_seq)
    _tabulate("replay (union of 3 runs)", replay)

    # Diff
    _diff("legacy_concurrent", legacy_conc, "legacy_sequential", legacy_seq)
    _diff("legacy (union)", legacy_conc | legacy_seq, "replay", replay)

    # Summary verdict
    legacy_union = legacy_conc | legacy_seq
    ghost_in_replay = replay - legacy_union
    missed_by_replay = legacy_union - replay
    print("\n" + "=" * 60)
    if not ghost_in_replay and not missed_by_replay:
        print("VERDICT: PERFECT MATCH — replay returns exactly the same slots as legacy")
    elif not missed_by_replay:
        print(f"VERDICT: replay returns SUPERSET ({len(ghost_in_replay)} extra slots)")
        print("         — these may be REAL slots that legacy misses (e.g.,")
        print("         times beyond the first one shown after click_day),")
        print("         OR ghost slots from parser issue. Manual verify any")
        print("         that look suspicious.")
    elif not ghost_in_replay:
        print(f"VERDICT: replay MISSES {len(missed_by_replay)} legacy slots")
        print("         — parser may be filtering too aggressively. INVESTIGATE.")
    else:
        print(f"VERDICT: BOTH directions differ "
              f"({len(ghost_in_replay)} ghost, {len(missed_by_replay)} missed)")
        print("         — inconsistencies need investigation.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
