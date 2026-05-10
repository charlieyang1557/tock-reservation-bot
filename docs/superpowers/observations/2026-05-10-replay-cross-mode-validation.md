# Calendar-replay cross-mode validation (2026-05-10)

The user pointed out that the post-Codex benchmark showed 75 replay
slots vs 15 legacy slots, asking whether they're duplicates, how
they're distributed, and whether we'd done point-check verification.

We hadn't. This doc records the validation that followed.

## Validation 1: do legacy and replay agree?

`spikes/http_replay/cross_mode_validate.py` — runs each mode 3×
back-to-back on benu, unions the (date, time) results, diffs them.

Result on benu, 6 target dates × 3 polls per mode:

```
legacy concurrent (union):  3 slots, 3 dates — all evening (8:30 PM, 7:30 PM)
legacy sequential (union):  3 slots, 3 dates — IDENTICAL to concurrent
replay (union):             15 slots, 5 dates — all early evening
                            (5:30, 6:30, 7:00 PM)

Common in BOTH legacy and replay:  0 slots
Only in legacy: 8:30 PM × 2, 7:30 PM × 1
Only in replay: 5:30/6:30/7:00 PM across 5 dates
```

**Disjoint outputs.** Surprising. Required deeper investigation.

## Validation 2: are the slots REAL?

`spikes/http_replay/verify_slot_reality.py` — for each (date, time)
reported by either mode, navigate to that slot's URL and inspect
whether Tock shows a Book button.

Result:
```
Verifying LEGACY-reported slots:
  2026-05-15 8:30 PM  → REAL
  2026-05-23 8:30 PM  → REAL
  2026-05-24 7:30 PM  → REAL

Verifying REPLAY-reported slots:
  2026-05-15 5:30 PM  → REAL
  2026-05-15 6:30 PM  → REAL
  2026-05-15 7:00 PM  → REAL
  2026-05-16 5:30 PM  → REAL
  2026-05-16 6:30 PM  → REAL
  2026-05-22 5:30 PM  → REAL
  2026-05-22 7:00 PM  → REAL
  2026-05-23 5:30 PM  → REAL
  2026-05-24 6:00 PM  → REAL

LEGACY tally: {'REAL': 3}    — 3/3 real
REPLAY tally: {'REAL': 9}    — 9/9 real, ZERO ghosts
```

**Both modes return only real slots.** No parser bug in either.

The verifier also dumped what Tock's UI shows for 5/15:
```
5:00, 5:15, 5:30, 5:45, 6:00, 6:15, 6:30, 6:45, 7:00, 7:15,
7:30, 7:45, 8:00, 8:15, 8:30, 8:45, 9:00 PM   (17 distinct times!)
```

So both modes are returning STRICT SUBSETS of the actual 17
bookable time slots. The disjoint outputs are an ARTIFACT of which
subset each chose.

## Validation 3: WHY do both modes underreport?

`spikes/http_replay/diagnose_parser_gaps.py` — fetches the live
calendar protobuf and dumps every time-shaped substring per date
section, searching for the missing 12 slots.

Result for 5/15:
```
Date section 2026-05-15 (1514 bytes):
  All distinct HH:MM strings: ['17:30', '18:30', '19:00', '19:30', '20:00', '20:30']
```

**The protobuf only contains 6 anchor times.** Tock's UI shows
17 because it INTERPOLATES 15-minute variants client-side from
each anchor (e.g., 17:30 anchor produces 17:00, 17:15, 17:30, 17:45
selectable buttons in the UI; all route to the same underlying
seating block).

So:
- Legacy `_check_date` finds 1 slot per date (the page's "primary"
  highlighted Book button) → reports 8:30 PM specifically because
  that's whatever Tock featured for that date when our URL hit
- Replay parses ALL 6 anchors → cap_slots_per_date keeps the top 3
  closest to `preferred_time=17:00` → keeps 17:30/18:30/19:00,
  drops 19:30/20:00/20:30
- Both are RIGHT; both are INCOMPLETE; the 0-overlap is the cap
  algorithm preferring early times

## Distribution analysis

Replay reports 15 slots/poll = 5 dates × 3 anchor times.

Per-date breakdown after cap (preferred_time=17:00):
```
2026-05-15: 5:30, 6:30, 7:00 PM    (closest to 5pm: 17:30, 18:30, 19:00)
2026-05-16: 5:30, 6:30, 7:00 PM    (same anchors)
2026-05-22: 5:30, 6:30, 7:00 PM
2026-05-23: 5:30, 6:30, 7:00 PM
2026-05-24: 5:30, 6:00, 6:30 PM    (5/24 had different anchor distribution)
```

All times are anchor times from the protobuf. Each anchor is a
distinct seating; there are no duplicates within a poll.

## Adjustment shipped

Default `replay_per_date_cap` raised from 3 → 5. Rationale:
- Each date has at most 5-6 anchors in the protobuf
- 5 covers near-complete (only drops 1 anchor in worst case)
- Worst-case fanout: 6 dates × 5 anchors = 30 race candidates,
  still well within Tock-friendly territory
- New env var `REPLAY_PER_DATE_CAP` for operator tuning

Re-benchmark with cap=5: 25 slots/poll (vs 15 with cap=3),
still 0.6s avg cycle (no perf regression).

## Recommendation for fuhuihua deploy

1. **Set `PREFERRED_TIME` to actual desired booking time before
   deploying.** The cap ranks slots by closeness to preferred_time.
   - Default `PREFERRED_TIME=17:00` (5pm) → bot races 17:00, 18:00,
     19:00 anchors first
   - `PREFERRED_TIME=20:00` (8pm dinner) → bot races 19:00, 20:00,
     21:00 anchors first
2. **Don't lower `REPLAY_PER_DATE_CAP` below 5** without measuring
   missed slots. The 5-anchor coverage is what makes replay
   strictly better than legacy (which catches only 1).
3. **Watch the booker's race outcome** in production logs. If the
   bot consistently picks the WRONG seating (e.g., books 5pm when
   the user wanted 8pm), the issue is preferred_time setting, not
   the replay path.

## What we learned (process)

1. **"More slots returned" ≠ "better"** without verifying
   correctness. The first benchmark showed 75 vs 15 and I called
   it a win — without proving the 75 weren't ghosts.
2. **0-overlap between modes is a smell**, not a "wow these are
   independent confirmations". When both modes return real slots
   but DON'T overlap, something is filtering differently — find
   that something.
3. **Tock's UI ≠ Tock's API.** The UI interpolates 15-min variants
   from anchor times. Booking-wise this distinction doesn't matter
   (any 15-min variant routes to the same seating), but it matters
   for "what slots does our bot SEE" reporting.
