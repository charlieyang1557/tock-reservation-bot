# Phase B3 Deferred Items (2026-05-10)

Codex adversarial review (one pass) on the B3 diff (`101190e..fcbe488`)
returned 0 Critical, 1 HIGH, 3 MEDIUM, 2 LOW.

Of those:
- **HIGH 1** (`_confirm_booking` shim bypassed the lock) — fixed in
  the follow-up commit. The shim now self-locks (`async with
  self._confirm_lock`) and respects `_confirm_attempted` so any
  future caller is automatically safe even when not going through
  `_book_single`.
- **MEDIUM 1** (payment-card wait amplified by N tasks) — fixed by
  adding an optional `booking_won: asyncio.Event` parameter to
  `_prepare_for_confirm`; loser tasks abort the 9-min reload loop on
  the next iteration when another task wins.
- **MEDIUM 2** (XHR telemetry per-line fsync) — fixed by batching all
  line writes before the single `flush()` + `fsync()`. Sniper-mode
  hot-path no longer pays N synchronous fsyncs per check_date.
- **MEDIUM 3** (PagePool returned closed pages) — fixed by validating
  `page.is_closed()` in a pop-and-discard loop in `acquire()`. Falls
  through to `browser.new_page()` when every pooled page is closed,
  AND schedules a refill so future acquires get a fresh page.

The findings below are deliberately deferred to a future phase.

## Deferred — to revisit in C planning

### 1. LOW — PagePool startup-failure leaves pool empty
**Source:** Codex review (`src/page_pool.py:98`).

`PagePool.start()` opens `target_size` blank pages via
`asyncio.gather(return_exceptions=True)`. If new_page() fails for one
or more (transient Cloudflare interstitial, OOM, etc.), the pool
silently has fewer pages than configured. Subsequent acquires fall
through to `new_page()` — losing the pool's perf benefit for the rest
of the run.

**Why deferred:** the failure mode is transient and recovery happens
implicitly via `_schedule_refill()` after each acquire. So a partial
startup self-heals over the first ~target_size acquires. Documenting
the gap rather than adding explicit recovery logic.

**Future improvement:** in `start()`, after the gather, if
`current_size < target_size`, schedule `target_size - current_size`
background refill tasks to fill the gap proactively rather than
waiting for the first acquire.

### 2. LOW — `xhr_telemetry.jsonl` is cwd-relative
**Source:** Codex review (`src/xhr_telemetry.py:32`).

The default log path is `Path("xhr_telemetry.jsonl")` — written to
the current working directory. Production runs from
`/Users/openclaw/tock-reservation-bot/`; tests run from worktrees
under `.claude/worktrees/<name>/`. If telemetry is enabled in a
shared environment by accident, test or worktree runs could write to
an unexpected production-looking file.

**Why deferred:** flag is OFF by default; no production data at risk
unless operator explicitly opts in. Operators who enable should also
verify the cwd.

**Future improvement:** make the path explicit in Config
(`xhr_telemetry_path: str`) or put it under a per-checkout
`./logs/` directory. Add a guard that refuses to write to a path
shared between worktrees (e.g., reject if `..` resolves to outside
the project root).

### 3. NOTE — `_book_single` is now ~280 lines
**Source:** Codex observation.

After B3.1 + B3.3 changes, `_book_single` orchestrates: page
acquisition (warm/handoff/pool/fresh), navigation, day click, slot
click, checkout wait, prep, lock-protected click+verify+soft-win,
release. The safety-critical confirm path is harder to audit at
this length.

**Why not extracted:** premature refactor. Each integration step in
`_book_single` is a distinct phase with its own context (different
abort checks, different cleanup invariants). Pulling them apart risks
losing the single-place audit story. Wait until C planning to decide
whether the extraction pays for itself.

**Future improvement (C planning):** extract `_acquire_booking_page`
(returns page + ownership flag) and `_run_confirm_under_lock`
(takes prep result + page) into separate methods. Keeps the orchestration
in `_book_single` thin and pushes details into named units.

## Lessons for C planning

1. **The HIGH was a self-inflicted regression we wouldn't have caught.**
   B3.1 introduced the shim with good intent (back-compat for tests).
   The shim's design — call prep + click directly without the lock —
   silently re-introduced the unsafe path the lock was added to
   prevent. Future helpers around safety-critical primitives should
   default to "uses the safety primitive" rather than "raw bypass for
   convenience."
2. **Concurrent waiters need an abort signal.** B3.1 made prep
   concurrent across N tasks but didn't pass `booking_won` through.
   The 9-min payment wait loop became N*4 reloads/min. Pattern:
   anything that loops with a sleep needs to check the win condition
   each iteration.
3. **Per-call disk fsync ≠ per-batch fsync.** Easy to write one fsync
   per record because `f.flush() + os.fsync()` reads naturally inside
   the loop. The right pattern in async hot paths is to batch writes
   then sync once.
4. **Cherry-pick from worktree subagents needs more conflict surface
   than expected.** B3.3 conflicted on 4 files (config, browser,
   booker, plan doc). Anticipate this and dispatch subagents to
   non-overlapping files OR rebase before cherry-pick.
