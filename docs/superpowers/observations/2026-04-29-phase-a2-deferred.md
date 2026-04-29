# Phase A+2 Deferred Items (2026-04-29)

Phase A+2 underwent three rounds of adversarial review. Pass 1 found 4
issues, pass 2 found 5, pass 3 found 4. After 3 rounds, the cycle was
hitting diminishing returns — Codex pass 3 itself recommended stopping
the review-fix cycle and shipping with documented gaps.

The 2 HIGH findings from pass 3 are fixed in commit daff47a. The 2 MEDIUMs
and 1 Disagreement below are deliberately deferred to Phase A+3.

## Deferred — to fix in Phase A+3 before next major architectural work

### 1. Sniper CF alert spam (MEDIUM)

**Source:** Codex pass 3, `src/checker.py:529-542`

During a sniper window, `check_all` runs every ~3 seconds for ~11 minutes.
The CF alert fires whenever the rolling rate exceeds threshold —
potentially every poll, producing up to ~220 duplicate Discord alerts
during the operator's highest-pressure recovery moment.

**Fix in Phase A+3:** add `_sniper_cf_alerted: bool = False` flag on
`AvailabilityChecker`. Set on first alert, reset in `close_sniper_pages()`.
Single Discord alert per sniper window.

**Why deferred:** the duplicate alerts are noisy but not data-loss. The
operator gets the signal regardless. Phase B's planned scanner/booker
split will consolidate this into the new event bus telemetry anyway.

### 2. No bounds validation on `PREWARM_MIN_DAYS_OUT` (MEDIUM)

**Source:** Codex pass 3, `src/config.py:101-104`

`PREWARM_MIN_DAYS_OUT=11` silently disables prewarm (no dates within the
10-day lookahead window). `=0` prewarms current-release dates that are
already booked or stale. `=-1` would crash some date arithmetic.

**Fix in Phase A+3:** in `load_config()`, validate `1 <= prewarm_min_days_out
<= prewarm_lookahead_days` and fail fast with a clear error. Same for
other numeric env-loaded fields that can produce silently-bad bot behavior.

**Why deferred:** the operator (one user) is unlikely to misconfigure this
in the next 1-2 release windows. Worth catching for general robustness
but not a release-blocker.

### 3. Restart as soft-win reset is the wrong primitive (Disagreement, partially addressed)

**Source:** Codex pass 3 disagreement.

Pass 3's HIGH 2 fix introduced `booking_uncertain.json` which addresses
the auto-restart bypass. But the operator clearance procedure is currently
manual: `rm booking_uncertain.json` after verifying on Tock.

**Future improvement:** add a CLI command `python main.py --clear-uncertain
--verified` that prompts the operator to confirm they verified on Tock,
then removes the file. Reduces the chance of an operator deleting the
file without actually checking.

**Why deferred:** the manual `rm` command is documented in the Discord
alert message and the bot logs. Adding a CLI flag is polish, not a
correctness fix.

## Lessons for future planning

The pattern of "each adversarial pass finds gaps in the previous fixes"
is informative. Three observations for Phase B:

1. **Adversarial review has converging marginal value.** Pass 1 found
   structural design holes. Pass 2 found gaps in pass 1's fixes. Pass 3
   found gaps in pass 2's fixes. The depth of finding decreased each
   round (HIGH count: 0 → 2 → 2; total findings: 4 → 5 → 4). At
   roughly equal cost per pass, marginal value is decreasing.

2. **Stop signal:** pass 3 explicitly flagged "scope is getting unwieldy;
   fix the high-risk blockers and document remaining gaps." Future
   adversarial review iterations should bake in a "stop after 2-3 passes"
   rule unless a CRITICAL is found in the latest pass.

3. **Two-stage Claude review missed all of pass 2's HIGHs and pass 3's
   HIGHs.** Cross-model adversarial review is providing real safety value.
   Consider making it standard for all SAFETY-CRITICAL features (booking,
   payment, race conditions) — not just architectural redesigns.

## Phase A+3 plan placeholder

When Phase B planning starts, consume this doc into the Phase A+3
follow-up sprint scope (or a Phase B preparatory cleanup commit).
Estimated effort: 30-60 minutes of work for items 1+2; 1-2 hours for
the optional item 3 CLI improvement.
