# 2026-04-25 — Phase A merged to main

## Status

Phase A (tactical fixes) is complete and merged to `main` at commit `dd45aa3`.
Pushed to `origin/main`. Branch `phase-a-tactical-fixes` deleted; worktree cleaned up.

**Spec:** [`docs/superpowers/specs/2026-04-24-tock-bot-architecture-redesign-design.md`](../specs/2026-04-24-tock-bot-architecture-redesign-design.md)
**Plan:** [`docs/superpowers/plans/2026-04-24-phase-a-tactical-fixes.md`](../plans/2026-04-24-phase-a-tactical-fixes.md)
**Investigation:** [`2026-04-14-poll-spam-incident.md`](2026-04-14-poll-spam-incident.md)

## What shipped

| Area | Change |
|---|---|
| Defense-in-depth | `src/process_lock.py` (singleton fcntl lock) + `src/poll_watchdog.py` (3-strike escalation) wired into `main.py` and `monitor.py`. |
| Slot extraction | `_collect_slots_multi` no longer emits `"Slot N"` placeholders. New `_extract_slot_time` tries 5 sources (child span / parent / 3 ancestors / aria-label / button text), drops the slot if none parse. |
| Selector scoping | New `slots_container` selector OR-list; slot collection scoped to it; debug-level fallback log to avoid sniper-window flood. |
| Sniper horizon | New `Config.sniper_scan_weeks: int = 2` field. `_get_target_dates(sniper_mode=keep_pages)` selects between normal and sniper horizons. |
| Test infrastructure | `tests/conftest.py` extracted with `make_page_locator` helper. 143 tests pass, 0 regressions. |

## Phase A+1 follow-ups (deferred from the final whole-branch review)

These items are real but not blocking. Pick up next session.

### Important

1. **`tests/test_scoped_slot_selectors.py` should use `make_page_locator` from `conftest.py`** — the helper was created precisely for this file but the file still defines its own page-locator closure with substring matching (the exact collision class the helper's exact-equality logic was designed to prevent). Refactor needs a second helper for the "container present" case (`make_page_locator_with_container`) — non-trivial design decision.

2. **End-to-end test for sniper-mode horizon flowing through `check_all`** — the unit tests in `tests/test_sniper_scan_weeks.py` cover `_get_target_dates(sniper_mode=True)` directly, but no test confirms `check_all(keep_pages=True)` actually applies the cap. Add one assertion in a sniper-mode test.

### Minor

3. Add `RotatingFileHandler` for `bot.log` — production log was 641 MB on Mac mini after ~12 days. Out of Phase A scope but worth scheduling.
4. Pre-existing dead code: `_is_day_available()` in `src/checker.py` has no callers. Remove during Phase B's scanner extraction.
5. The `keep_pages` flag in `check_all` is overloaded across three concerns (page lifetime, abort behavior, scan horizon). Phase B's introduction of explicit `Mode` typing should decouple.

## Phase B gate

Per the spec, **Phase B (scanner/booker split + page-state machine) is gated on observing one Friday release window with Phase A in production.** The spec criteria:

- No `Slot N` label appears in any production log for 1 release window.
- Singleton lock + watchdog observed firing in test, dormant in production.
- `--verify` still passes.
- Apr-14-style log burst does not recur in 2 release windows post-A.

After the next Friday window, append a short observation note to `docs/superpowers/observations/<YYYY-MM-DD>-window-1.md` covering: did anything new break, did the fixes hold, did the slot get booked, what surprised you. Phase B's writing-plans handoff uses that note.

## Operational deployment notes (Mac mini)

The Apr-24 investigation confirmed two `python main.py` processes had been running for 12 days. Operator killed one (PID 41961, the orphan) on 2026-04-24. After deploying this branch:

1. Pull on Mac mini: `cd ~/tock-reservation-bot && git pull`.
2. Kill any current bot processes: `pm2 delete all` (if PM2) AND `pkill -f 'python main.py'` (catches non-PM2 strays).
3. Start fresh — singleton lock will refuse any second start.

If running under PM2, see the `ecosystem.config.js` recommendation in the 2026-04-25 chat session (key flags: `max_restarts: 10`, `min_uptime: '60s'`, `restart_delay: 5000`).

## Production cleanup needed (independent of code)

- Truncate the 641 MB `bot.log`: `> bot.log` while bot is stopped.
- Delete any stale `bot.lock` files: `rm bot.lock` (singleton lock will recreate on next start; reclaim is automatic for stale PIDs anyway).
