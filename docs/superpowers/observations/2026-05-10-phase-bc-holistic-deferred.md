# Phase B+C Holistic Review Deferred Items (2026-05-10)

After per-phase Codex reviews of B1, B2, B3 (each addressed) and the
C spike infrastructure shipping with no review, a HOLISTIC pass on
the cumulative diff `1a528b4..HEAD` (18 commits) ran. The first pass
exhausted its budget on file inspection without final findings; a
focused 2nd pass ("safe wrapper semantics, cwd-scoped state, weakest
test") returned 3 items.

## Addressed in the follow-up commit

- **Latent issue: `_confirm_booking` shim semantically weaker than
  `_book_single`.** Found unused in `src/`. Was previously locked in
  the B3 Codex fix, but missing the soft-win persistence + notify +
  shared `booking_won.set` that `_book_single` does. Fix: deleted the
  shim and updated 3 `test_skip_day_click.py` patches + removed the
  2 `test_codex_b3_fixes.py` shim-safety tests. Future code that
  needs to confirm goes through `_book_single` only.

## Deferred — to revisit before next major phase

### 1. cwd-scoped state pollution from test modes (MEDIUM)
**Source:** Holistic review.

Several runtime-state files are cwd-relative:
  - `bot.lock` (process_lock)
  - `session_cookies.json` (browser)
  - `slot_tracker.json` / `.csv` (tracker)
  - `booking_uncertain.json` + `booking_uncertain.archive/`
  - `selector_metrics.json` + `.tmp`
  - `xhr_telemetry.jsonl`

Confirmed safe paths:
  - `--test-booking-flow` does NOT call `write_uncertain` (the
    booker is never instantiated; navigation stops before confirm).

Confirmed risky paths:
  - `--test-sniper-benchmark` calls real `tracker.record(...)` and
    writes `slot_tracker.{json,csv}` to whatever cwd the bot was
    launched from. If the operator runs benchmarks from a worktree
    while production runs from the repo root, the worktree gets a
    PARTIAL slot tracker (or production loses the test entries).
  - All test modes acquire `bot.lock` in cwd. Safe in isolation but
    means a worktree run cannot lock against production.
  - `session_cookies.json` may be saved by login if cookies refresh
    during the test run — could overwrite the production file if
    cwd points to it.

**Why deferred:** the highest-risk case (`booking_uncertain.json`)
is provably safe. Other writes are nuisance, not data loss. Fix is
a real refactor — introduce `TOCK_STATE_DIR` env var that all
runtime-state file paths honor, default to cwd. ~50 LOC across
5 modules + matching test updates. Worth doing before any operator
ever runs a test from a non-production cwd; document in operator
runbook in the meantime.

**Operator workaround until fixed:** when running `--test-*` modes,
either:
  a. Run from the production checkout (after stopping production), OR
  b. Run from a worktree with `.env` and `session_cookies.json`
     symlinked/copied; expect `slot_tracker.*` and `bot.lock` to be
     written to that worktree (harmless).

### 2. Weakest test: real-Playwright JS algorithm coverage (MEDIUM)
**Source:** Holistic review (re-flagged what B1 deferred doc HIGH 3
already noted).

`test_detects_via_dom_when_url_is_clean` mocks `page.evaluate`
returning True. The actual `_CF_DOM_DETECT_JS` in `src/checker.py`
could be syntactically broken or query the wrong DOM and this test
would still pass. Same shape applies to all the B1.2 / B1.3 / B2.2
JS tests (~30 tests across the branch).

**Why deferred:** real-Playwright fixtures need a Chromium context
per test, ~few-hundred-line infrastructure addition. Tracked in
`docs/superpowers/observations/2026-05-09-phase-b1-deferred.md`
already. Re-flagging here so the next planning cycle treats it as
a P1 not a P3.

**Suggested fix pattern (per Codex):** add a new test fixture
that uses `page.set_content()` with representative HTML snippets
for each JS scenario:
  - `iframe[src*="challenges.cloudflare.com"]` present
  - `.cf-turnstile` widget present
  - "Verify you are human" text in a visible h1
  - clean Tock-like search page (negative case)

Then call `await checker.is_cloudflare_challenge_page(page)` for
real and assert the boolean. Same pattern works for
`_CLICK_TIME_SLOT_JS` (button-with-time-text) and
`_COLLECT_SLOTS_JS` (5-source extraction).

## Lessons (cross-phase)

1. **Back-compat shims for safety primitives are footguns.** B3
   added `_confirm_booking` "in case future callers need it"; the
   holistic review caught it as semantically weaker than the live
   path before any caller used it. Pattern: don't add convenience
   wrappers around safety primitives unless there's a current
   caller that needs them.
2. **cwd-scoped state surfaces during multi-checkout dev workflows.**
   Single-cwd production was fine; once anyone runs from a worktree
   for testing, the implicit assumption breaks. Future state files
   should default to a config-driven directory.
3. **Mocking `page.evaluate` keeps test counts up but coverage
   thin.** B1, B2, B3 all relied on the same pattern. The
   real-Playwright fixture investment is now a recurring cost; pay
   it once before the next phase that adds JS.
