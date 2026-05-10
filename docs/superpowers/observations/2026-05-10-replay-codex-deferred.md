# Calendar-replay Codex review deferred items (2026-05-10)

Codex pass on the calendar_replay integration (`4b30493..2c0170b`)
returned 0 Critical, 2 HIGH, 4 MEDIUM, 2 LOW.

## Addressed in the follow-up commit

- **HIGH 1: booker fan-out from 145 replay slots** — fixed via
  `cap_slots_per_date(slots, preferred_time, per_date_cap=3)`. Caps
  output to top 3 slots per date, ranked by closeness to
  `preferred_time`. Worst case: 6 dates × 3 = 18 booking tasks.
  Empirically slots drop 145 → 75 over 5 polls (~15/cycle), well
  inside Tock-friendly fan-out.

- **HIGH 2: 200 + HTML CF interstitial suppresses fallback** — fixed
  via `body_looks_protobuf(body)` check in `fetch_calendar`. Detects
  HTML signatures (`<!doctype`, `<html`, `<head`) and bodies smaller
  than 20 bytes. On suspicious 200, returns None so check_all falls
  through to the legacy DOM scan path.

- **MEDIUM 4: replay failures hide stale-auth situations** — fixed
  via circuit breaker:
  `_REPLAY_FAILURE_THRESHOLD = 3`. After 3 consecutive failures
  (init or fetch), `_replay_circuit_open=True` and subsequent calls
  to `_try_calendar_replay` short-circuit to None without
  init/fetch. Reset on `close_replay_session()` so the next sniper
  window starts fresh.

## Deferred — to revisit before deploying replay to fuhuihua

### MEDIUM 1: Parser pairs date/time by byte range, not protobuf structure
**Source:** Codex review (`src/calendar_replay.py:232`).

Current parser scans bytes linearly: each date marker starts a
"section", times in that section are attributed to the date. If
Tock ever:
- Adds an unrelated `YYYY-MM-DD` field anywhere in the body (e.g., a
  business open-date), it becomes a phantom date section
- Reorders fields so date appears AFTER its slot times, attribution
  flips wrong

**Why deferred:** the empirical benu body has dates in declaration
order followed by their slots; verified across 5 successive replays
and 14 dates. Risk is low for the immediate Friday release but
could break on a Tock schema update.

**Future improvement:** decode the protobuf properly (use
`protobuf` library + an inferred `.proto` schema). Or: maintain a
golden-body fixture per restaurant; before each release window,
diff the live body shape against the golden — fail closed to legacy
if structural markers change.

### MEDIUM 2: `_MIN_BOOKING_HOUR = 10` drops brunch slots
**Source:** Codex review (`src/calendar_replay.py:262`).

The hard 10:00 lower bound filters out genuine 9:00/9:30 AM brunch
slots if any restaurant offers them. Fuhuihua and benu are dinner-
only so this isn't an immediate concern.

**Future improvement:** make `_MIN_BOOKING_HOUR` configurable per
restaurant via a Config field, OR drop the heuristic entirely once
we can rely on protobuf structure to disambiguate slot times from
framing bytes.

### MEDIUM 3: Dedupe loses distinct experiences at same date/time
**Source:** Codex review (`src/calendar_replay.py:249`).

Tock can offer multiple experiences per restaurant ("Dining Room"
vs "Lounge"). Both at "5:00 PM Friday" would collapse to one
`AvailableSlot` after dedupe. Fuhuihua has a single experience
("Tasting Menu") so this is not an immediate issue.

**Future improvement:** preserve the experience identifier from the
protobuf (if reachable). If two distinct experiences match at the
same time, return both as separate slots so the booker can target
the preferred one.

### LOW 1: Captured headers replayed wholesale
**Source:** Codex review (`src/calendar_replay.py:113`).

We replay every captured header including `cookie`, `referer`,
`origin`. Chromium should override `Cookie` via the credentials:
include flag, but relying on captured headers here is brittle.

**Future improvement:** allowlist the headers that Tock actually
checks (the `x-tock-*` family + `accept` + `content-type`). Drop
`cookie`, `referer`, `origin`, `host` before replay.

### LOW 2: `fetch_calendar` doesn't pre-check `page.is_closed()`
**Source:** Codex review (`src/calendar_replay.py:151`).

If the page has been closed externally (browser shutdown, crash),
`page.evaluate` raises and the existing try/except handles it. Not
a correctness bug, just one wasted poll on a known-broken state.

**Future improvement:** add `if session.page.is_closed(): return None`
at the top of `fetch_calendar` for slightly cleaner failure path.

## Notes carried forward

- The current integration tests mock `fetch_calendar` and
  `initialize_replay_session`. The parser tests use handcrafted byte
  strings (one is real benu shape). They DO NOT cover:
  - 200 + HTML body (now exercised by the new HIGH 2 test)
  - Real fuhuihua body shape (assumed similar to benu)
  - Multi-experience duplicate times
  - Stale header behavior over a long window

- The live validation script (`spikes/http_replay/validate_replay.py`)
  runs only against benu. Before the first fuhuihua release deploy:
  1. Run `validate_replay.py` against fuhuihua to confirm the same
     shape (benu's `businessId=10775`, fuhuihua's will differ —
     captured automatically by `initialize_replay_session`)
  2. Cross-check the captured `x-tock-scope` JSON for fuhuihua's
     businessId
  3. Confirm parsed slot times match what the operator sees in a
     real headed browser session

- The replay strategy is one technological generation ahead of
  what Tock's anti-bot is currently checking. If Tock tightens
  fingerprint validation per-request OR rotates session tokens
  aggressively, the strategy could break. The circuit breaker +
  legacy fallback ensure missed-release safety even in that case.
