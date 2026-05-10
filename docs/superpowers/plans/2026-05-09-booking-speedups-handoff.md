# Handoff Prompt — Booking Speedups Implementation

Paste the prompt below into a fresh Claude Code session (in this same worktree) when you're ready to start implementation.

---

## Prompt

You're picking up implementation of **Phase B1** of the booking-speedups plan in this Tock reservation bot. A prior session did the architectural review and wrote the plan; your job now is to execute.

### First three things to do, in order

1. Read `CLAUDE.md` end-to-end. **The TDD requirement is mandatory** — write failing tests before implementation, every time.
2. Read `docs/superpowers/plans/2026-05-09-booking-speedups.md`. That's the plan. The recent commits at HEAD already shipped the "normal-mode fast-path handoff" change and the Codex-driven hardening of it. Phase B1 is what you're implementing now.
3. Read `git log --oneline -20` and `git diff main --stat` so you know what's already been touched.

### What's been done

- Normal-mode fast-path handoff: checker stops scanning on first slot, parks the live page, booker reuses it.
- Codex adversarial review pass 1 + 2: strict_time_match for warm pages, try/finally cleanup in monitor, close-on-overwrite for handoff dict.
- 226 tests passing. New tests in `tests/test_normal_fast_handoff.py`.
- Untracked but should be staged: `tests/test_normal_fast_handoff.py` and `docs/superpowers/plans/2026-05-09-booking-speedups*.md`.

### What you're doing this session

Implement **Phase B1** tasks **in order**: B1.1 → B1.2 → B1.3 → B1.4 → B1.5.

Do not skip ahead to B2, B3, or C. They're independent and can be separate sessions. Phase B1 is the highest-confidence quick wins. Estimated 3–5 days of work; **stop and ask** if you hit a blocker that would push past 1.5x.

For each task:
1. Read the relevant `src/` file at the line ranges noted in the plan.
2. Write failing tests (the plan lists them by name). Run them — they should be red.
3. Implement the change.
4. Run the new tests — they should be green.
5. Run `pytest tests/ -q` — all 226+ tests must still pass.
6. **Mark the task complete in the plan file with a one-line note** (no separate doc).

### Working environment

- Worktree: `/Users/openclaw/tock-reservation-bot/.claude/worktrees/epic-agnesi-64d6e7`
- Branch: `claude/epic-agnesi-64d6e7`
- venv at `/Users/openclaw/tock-reservation-bot/venv` (use `/Users/openclaw/tock-reservation-bot/venv/bin/python -m pytest`)
- Don't activate venv with `cd` — use absolute paths. Don't `source venv/bin/activate` because the shell state doesn't persist.
- Existing test fixtures in `tests/conftest.py` (especially `_clean_booking_uncertain_file`) — use them.

### Ground rules (project-wide)

- **TDD non-negotiable.** Tests first, fail-then-pass-then-commit. Per `CLAUDE.md`. The prior session was caught not committing tests; if you write new test files, `git add` them as part of your final commit.
- **No real bookings.** All work happens with `dry_run=True`. The `--test-*` CLI flags force dry-run; use them for integration testing. Never invoke `python main.py` in normal mode.
- **No reading `.env` or `*.pem`.** They contain credentials.
- **Don't touch sniper warm-page code unless a B1 task explicitly requires it.** Sniper has its own race-all-slots semantics (`_sniper_pages`, `pop_warm_page`) that work and shouldn't be merged with the new `_handoff_pages` path.
- **Preserve booking guards.** `asyncio.Lock` around the confirm click, `_confirm_attempted` event, `booking_uncertain.json` persistence — all stay. (B3.1 will move the LOCK BOUNDARY but doesn't remove the lock; that's a later session.)
- **Selector changes go through `src/selectors.py`.** Don't inline new selectors in checker/booker.
- **Logging stays on info level for hot-path events.** Don't downgrade `[check] First slot found` or `[book] using warm page`.

### Specific notes per task

**B1.1 — `_wait_for_checkout` race-of-waiters:** the function returns `True` on success and `False` on full timeout. The new implementation must preserve that contract; `asyncio.wait` returns `(done, pending)` and you have to handle both. Cancel pending tasks; await them (Python warns about un-awaited cancellations). Also: don't drop the screenshot-on-timeout call at the bottom of the original function.

**B1.2 — batch `_click_time_slot`:** the `strict_time_match` parameter still applies. JS function should accept `strictTimeMatch` and respect it (no fallback when True). The function signature in Python stays the same; only the internals change.

**B1.3 — batch `_collect_slots_multi`:** the `slots_container` scoping must stay (Apr 17 lesson — global "Book" buttons are false positives). Do the container scoping in JS; if container not found, fall back to page-wide as today.

**B1.4 — networkidle removal:** safe deletion, but verify by running `python main.py --once` once headlessly to confirm warm_session still works. The `try/except` around the call already handles the timeout silently — you're just removing that whole try block.

**B1.5 — skip `_click_day` A/B test:** **don't flip the default to True in this session.** Add the config flag, the conditional logic, the fallback, and the tests. Default stays False. The flip happens later after a real-release run.

### When done

1. `pytest tests/ -q` — must show all tests passing.
2. `python -m compileall -q src tests` — must be clean.
3. Stage and commit with a message that lists the B1.* tasks completed.
4. Print a short summary: which B1 tasks shipped, latency wins observed (if any benchmarks ran), what remains for B2/B3/C.
5. **Do not invoke `gh pr create` or push** — leave that to the operator.

### If something is unclear

The prior session's analysis lives in two places:
- `docs/superpowers/plans/2026-05-09-booking-speedups.md` — the plan
- The conversation that produced it isn't loaded in this session

If a plan task's intent feels under-specified, prefer the **smallest-blast-radius** interpretation: change the smallest thing that makes the new test green, leave the rest alone. The prior session's tone was "minimal, well-tested changes" and the project's `CLAUDE.md` reinforces that.

If you hit a real architectural ambiguity, **stop and ask** before writing code. Don't bake decisions into commits that the operator hasn't seen.

---

End of handoff.
