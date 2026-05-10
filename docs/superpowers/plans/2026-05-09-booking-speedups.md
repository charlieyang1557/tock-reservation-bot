# Plan — Booking Speedups (Phase B & C)

**Created:** 2026-05-09
**Author:** prior-session synthesis (Claude + Codex adversarial review)
**Branch base:** `claude/epic-agnesi-64d6e7` (current worktree, contains the normal-mode fast-path handoff change)
**Scope:** Reduce slot-detection-to-confirm-clicked latency from ~1–4 s to <1 s where possible.

## Goal

After the recent normal-mode fast-path change, the booker can claim a live page from the checker so it doesn't have to re-navigate. The remaining latency on the critical path is dominated by **DOM polling, fixed sleeps, and per-button round-trips**. The bot is DOM-bound when it could be event/network-bound.

This plan is structured in three phases ordered by risk-adjusted ROI. Each phase is an independent shippable unit; later phases assume the earlier phases landed.

## Context

- The bot just shipped two PRs:
  - "normal-mode fast-path handoff" (parks the live page that found a slot so the booker reuses it)
  - "Codex adversarial review fixes" (strict_time_match on warm pages; try/finally cleanup; close-on-overwrite)
- TDD is **mandatory** per `CLAUDE.md` — write failing tests first.
- Real bookings are gated by `--dry-run` and `DRY_RUN=true`. **Never test against the live restaurant without dry-run on.**
- The bot races against other automated bookers in a 3–10 second window after release.

## Phase B — DOM and Architecture Speedups (low–medium risk)

### B1 — Quick DOM wins (target: 3–5 days)

#### B1.1 — Replace `_wait_for_checkout` polling with race-of-waiters ✅ Done 2026-05-09 (uses `asyncio.as_completed`; 7 new tests in `test_wait_for_checkout_race.py` pass in 0.10 s vs 25 s on the old impl)
**Saves:** 0–1900 ms per booking.
**Files:** [src/booker.py:597–654](src/booker.py:597)
**Approach:** Replace the 2-s outer poll with `asyncio.wait([...], return_when=FIRST_COMPLETED)` over three primitives: `page.wait_for_url(url_predicate, timeout=30000)`, `page.wait_for_selector(checkout_container, timeout=30000)`, `page.wait_for_function(payment_visible_js, timeout=30000)`. Cancel the losers when the winner returns.
**Tests to write first:**
- `test_wait_for_checkout_returns_on_url_change_first`
- `test_wait_for_checkout_returns_on_selector_first`
- `test_wait_for_checkout_returns_on_payment_element_first`
- `test_wait_for_checkout_returns_false_when_all_time_out`
- `test_wait_for_checkout_cancels_losing_waiters` (no warnings about un-awaited coroutines)
**Done when:** existing booking flow tests still pass; new tests pass; mean checkout-detection latency drops measurably in `--test-booking-flow` runs.

#### B1.2 — Batch `_click_time_slot` into one `page.evaluate` ✅ Done 2026-05-09 (single JS round-trip; 9 new tests in `test_click_time_slot_batched.py`; 4 legacy `test_slot_click.py` + 2 booking-fixes tests + 1 fast-handoff test rewritten to mock `page.evaluate`; `re` import dropped)
**Saves:** 100–800 ms per booking task.
**Files:** [src/booker.py:441–595](src/booker.py:441)
**Approach:** Replace the per-button text_content/click loop with one JS function that takes `(target_time, generic_selectors)` and returns `{clicked: bool, text: str, reason: str}`. JS does substring + regex match in a single round-trip; click happens inside the same evaluate. Keep `strict_time_match` semantics: when True, refuse the first-button fallback in JS.
**Tests to write first:**
- `test_click_time_slot_clicks_exact_time_match`
- `test_click_time_slot_clicks_regex_time_match`
- `test_click_time_slot_clicks_generic_button_when_parent_has_time`
- `test_click_time_slot_skips_generic_button_when_parent_lacks_time`
- `test_click_time_slot_strict_mode_refuses_fallback`
- `test_click_time_slot_non_strict_clicks_first_specific_button`
**Done when:** all existing `test_slot_click.py` and `test_race_all_slots.py` tests still pass.

#### B1.3 — Batch `_collect_slots_multi` into one `page.evaluate` ✅ Done 2026-05-09 (single JS round-trip handles container scope + 5-source extraction; 7 new tests in `test_collect_slots_multi_batched.py`; legacy tests in `test_checker_detection.py`, `test_scoped_slot_selectors.py`, `test_slot_labeling.py` rewritten to mock `page.evaluate`; `_extract_slot_time` removed)
**Saves:** 150–1000 ms per detected date.
**Files:** [src/checker.py:1015–1101](src/checker.py:1015)
**Approach:** Same pattern — one JS pass over `page.locator(matched_selector)` that does the 5-source time extraction (slot_time_text span → parent text → ancestors → aria-label/title → button text) and returns `[{time: str | null, source: int}, ...]`. Drop slots with `time == null` (preserves the Apr 17 lesson: never fabricate "Slot N").
**Tests:** existing `test_checker_detection.py::TestCollectSlotsMulti` covers the cases — keep them green; add `test_collect_slots_multi_single_evaluate_call` to assert one round-trip.
**Done when:** all 19 detection tests pass; benchmark in `--test-sniper-benchmark` shows reduced per-date scan time.

#### B1.4 — Replace `wait_for_load_state("networkidle")` in `warm_session` ✅ Done 2026-05-09 (call dropped entirely; 2 new tests in `test_warm_session_no_networkidle.py` assert no networkidle wait + domcontentloaded preserved on goto)
**Saves:** ~5 s per warm cycle (the timeout fires every cycle on Tock's main page).
**Files:** [src/browser.py:263](src/browser.py:263)
**Approach:** Drop the call entirely. Tock's main page has third-party analytics that never reach networkidle; the fixed 5-s timeout is pure waste. Optionally replace with `wait_for_function("document.readyState==='complete'", timeout=2000)`.
**Tests:** add `test_warm_session_does_not_block_on_networkidle` — patch `wait_for_load_state` and assert it's not called (or called with a different state).
**Done when:** test passes; manually verify warm cycle is faster via `python main.py --once` with HEADLESS=true.

#### B1.5 — Skip `_click_day` when URL date is authoritative ✅ Done 2026-05-09 (config flag `skip_day_click_check` defaults False; checker `_check_date` and booker `_book_single` both honor it with a single bounded retry; 8 new tests in `test_skip_day_click.py`. Default flip pending a real release-window A/B run)
**Saves:** 50–300 ms per scan.
**Files:** [src/checker.py:679](src/checker.py:679), [src/checker.py:1023](src/checker.py:1023), [src/booker.py:_click_calendar_day]
**Approach:** Tock's `/search?date=YYYY-MM-DD` URL may already select that date in the SPA, making the subsequent calendar-day click redundant. **Don't blindly cut it** — A/B-test:
1. Add `config.skip_day_click_check: bool = False` (default).
2. When True, `_check_date` calls `_collect_slots_multi` directly without `_click_day`. If 0 slots, fall back to clicking and retrying.
3. Run for one full release window (Friday 7:59 PT). If hit rate matches, default to True.
4. Same logic in `_book_single` — try the slot click first; if no buttons, fall back to clicking the day.
**Tests to write first:**
- `test_check_date_skip_day_click_finds_slots`
- `test_check_date_skip_day_click_falls_back_when_no_slots`
- `test_book_single_skip_day_click_then_clicks_slot`
**Done when:** A/B run completes with no missed releases; default flips to True.

### B2 — Robustness improvements (target: 2–3 days, can run alongside B1)

#### B2.1 — Expire stale `booking_uncertain.json` by date ✅ Done 2026-05-10 (read_uncertain archives any file with `slot_date_str` >7 days past or malformed to `<path>.archive/<ts>_<reason>__<basename>`; 7 new tests in `test_uncertain_stale_expiry.py`)
**Files:** [src/booking_uncertain.py:58](src/booking_uncertain.py:58)
**Approach:** In `read_uncertain()`, parse `slot_date_str` as `date`. If the date is more than 7 days in the past, log a warning and return None (the file is stale; don't block future races). Auto-archive the file to `booking_uncertain.archive/<timestamp>.json` instead of deleting, so operators can audit.
**Tests:**
- `test_read_uncertain_returns_none_for_stale_date`
- `test_read_uncertain_archives_stale_file`
- `test_read_uncertain_keeps_recent_file`
**Done when:** old uncertain files don't block today's races; archive directory contains the original.

#### B2.2 — CF challenge detection beyond URL ✅ Done 2026-05-10 (new async `is_cloudflare_challenge_page` combines URL + DOM iframe/turnstile/interstitial-text via `_CF_DOM_DETECT_JS`; sync `_is_cloudflare_challenge_page` preserved for legacy fast-path callers; 8 new tests in `test_cf_detection_dom.py`; legacy prewarm + CF telemetry tests updated to mock `evaluate=False`)
**Files:** [src/checker.py:234–256](src/checker.py:234) (`_is_cloudflare_challenge_page`)
**Approach:** Add a DOM check via `page.evaluate`:
```js
() => !!document.querySelector(
  'iframe[src*="challenges.cloudflare.com"], '
  + '.cf-turnstile, '
  + '#cf-please-wait, '
  + '#cf-spinner-please-wait'
) || !!Array.from(document.querySelectorAll('h1,h2,p,div')).find(
  el => /verify you are human|just a moment|checking your browser/i.test(el.innerText || "")
)
```
Returns True if EITHER the URL match OR the DOM signal fires. Cache the result per (page, navigation) since DOM stability matters less than freshness here.
**Tests:**
- `test_cf_detection_url_only` (existing)
- `test_cf_detection_iframe_present`
- `test_cf_detection_turnstile_widget`
- `test_cf_detection_text_signal`
**Done when:** signals fire on staged challenge pages; live test with a known CF challenge confirms detection.

#### B2.3 — Stripe iframe URL caching ✅ Done 2026-05-10 (per-selector `_frame_url_cache: dict[str, str]` on TockBrowser; matching frames tried first, falls through to full scan; skips detached frames; 7 new tests in `test_find_in_frames_cache.py`)
**Saves:** 75–400 ms per CVC interaction.
**Files:** [src/browser.py:307](src/browser.py:307) (`find_in_frames`)
**Approach:** Add an instance cache `self._cvc_frame_url_pattern: str | None`. On first successful match, capture `frame.url` and store the URL prefix. On subsequent calls, check frames matching the cached pattern first; fall through to full scan on miss.
**Tests:**
- `test_find_in_frames_caches_after_first_match`
- `test_find_in_frames_falls_through_on_pattern_miss`
- `test_find_in_frames_clears_cache_on_invalidation` (e.g., new context)
**Done when:** find_in_frames performance test shows reduction on subsequent calls.

#### B2.4 — Selector hit telemetry ✅ Done 2026-05-10 (new `src/selector_metrics.py` with thread-safe in-memory counter + atomic JSON flush; hooks in `checker._check_date` and `booker._click_time_slot` keyed by `slot_button_check`/`slot_button_book`; monitor flushes after every poll outside sniper mode and every 5th poll inside; `--selector-stats` CLI prints top selectors per key; 18 new tests in `test_selector_metrics.py`)
**Files:** new `src/selector_metrics.py`
**Approach:** Tiny module with `record_match(key: str, selector: str)` that appends to `selector_metrics.json` (lazy in-memory aggregation, periodic flush). Hook into checker and booker on every successful selector match. Add a CLI flag `--selector-stats` to print top selectors per role.
**Tests:** standard append-and-read tests; cleanup in `conftest.py`.
**Done when:** after one production run, the JSON shows selector hit counts; team can re-order fallbacks by data.

### B3 — Architecture changes (medium risk; target: 3–5 days)

#### B3.1 — Move confirm-lock granularity ✅ Done 2026-05-10 (split into `_prepare_for_confirm` (no lock — payment detect, CVC fill, wait for confirm button) and `_execute_confirm_click_and_verify` (under lock — click + verify); 7 new tests in `test_confirm_lock_split.py` covering concurrent prep, lock-protected click, _confirm_attempted blocking, 5-way no-double-booking fuzz; `_confirm_booking` retained as backwards-compat shim)
**Saves:** 200–1500 ms in races where multiple slots reach checkout simultaneously.
**Files:** [src/booker.py:325](src/booker.py:325) (`_book_single`'s lock block)
**Approach:** Currently the lock wraps `_confirm_booking` which includes payment detection, frame search, CVC fill, and the click. Split into:
- **Prep (no lock):** payment detection, CVC fill, wait for confirm button to be visible. Runs concurrently across racing tasks.
- **Click (locked):** `async with self._confirm_lock` only wraps the actual `await page.click(confirm_button)` and the `_confirm_attempted` flip. Verification (post-click) stays inside the lock for the soft-win bookkeeping.

The existing `_confirm_attempted` event already provides session-level deduplication. Splitting is safe because:
- Prep is idempotent (CVC fill is per-page, not per-session)
- Click is the only step that creates a booking
- Verification of the click outcome stays under the lock so soft-win is recorded atomically

**Tests to write first:**
- `test_two_tasks_prepare_concurrently_then_one_clicks` — assert prep awaits run interleaved
- `test_only_one_task_executes_confirm_click_under_lock` — existing assertion preserved
- `test_confirm_attempted_blocks_second_click_after_prep` — second task that finishes prep sees the flag and aborts
- `test_no_double_booking_under_high_concurrency` — fuzz with 5 racing slots, exactly 1 click

**Done when:** existing `test_race_all_slots.py` still passes; new tests verify concurrent prep + serialized click; benchmark shows races finish faster.

#### B3.2 — Event-driven slot detection via `page.expect_response` ⚠️ Done 2026-05-10 — TELEMETRY-ONLY first pass (config flags `event_driven_detection` + `event_driven_url_pattern` default OFF; new `src/xhr_telemetry.py::XhrTelemetryRecorder` registers a Playwright `response` listener during `_check_date` and writes matching XHRs to `xhr_telemetry.jsonl` for operator analysis; 9 new tests in `test_event_driven_detection.py`. Operator pre-work to identify Tock's actual slot-availability XHR pattern is now possible without code changes; the JSON-parser fast-path is a follow-up commit once the pattern is known)
**Saves:** 100–300 ms per slot detection (DOM paints AFTER the network response arrives).
**Files:** [src/checker.py:_check_date](src/checker.py:597), specifically the slot-detection block after `_click_day`.
**Approach:** Wrap the day click in `async with page.expect_response(predicate, timeout=...) as resp_info: await page.evaluate(click_js)`. The predicate matches Tock's slot-availability XHR (need to identify it via DevTools — likely contains `availability`, `slots`, or the date in the URL). When the response lands, parse the JSON directly (skip DOM) for the slot list. Fall back to DOM scan if response shape is unrecognized.

**Pre-work needed:** Identify the actual XHR pattern. Run `python main.py --test-booking-flow` headed mode, open DevTools → Network, click a calendar day, record the URL pattern of the slot-data response. Document in this plan as discovered.

**Tests to write first:**
- `test_check_date_event_driven_returns_slots_from_response_json`
- `test_check_date_event_driven_falls_back_to_dom_when_response_missing`
- `test_check_date_event_driven_handles_response_timeout`

**Done when:** detection is 100+ ms faster on average across 10 polls; fallback to DOM works when XHR shape changes.

#### B3.3 — Page pool for race overflow ✅ Done 2026-05-10 (new `src/page_pool.py::PagePool` with deque-backed pool + semaphore-capped lazy refill via `asyncio.create_task`; `target_size=4` configurable via `PAGE_POOL_SIZE` env, `0` disables; `_book_single` uses `pool.acquire()` when no warm page and releases on finally; `close_all()` cancels in-flight refills; 17 new tests in `test_page_pool.py`)
**Saves:** 200–500 ms per cold task (only matters in multi-slot races).
**Files:** new `src/page_pool.py`
**Approach:** `PagePool` class with `target_size = 4` (configurable). On startup, opens N blank pages with stealth applied. `acquire()` returns a pre-warmed page or creates a fresh one if pool empty. `release(page)` closes the page (DOM state is dirty after a race attempt). Pool refills lazily up to target_size.

Wire into `_book_single` to call `pool.acquire()` instead of `browser.new_page()` when no warm page is available.

**Tests:**
- `test_page_pool_returns_prewarmed_page_when_available`
- `test_page_pool_creates_fresh_page_when_empty`
- `test_page_pool_refills_after_release`
- `test_page_pool_respects_target_size`

**Done when:** during a sniper race, second-attempt-cost is measurably lower; pool fills opportunistically without blocking.

## Phase C — Raw HTTP Spike (high risk, headline win, target: 1–3 days for spike)

This is the headline opportunity but has highest uncertainty. Time-box strictly.

### C.0 — Reconnaissance (≤4 hours) ✅ Done 2026-05-10 (CLI `python -m spikes.http_replay.recon --restaurant SLUG --date YYYY-MM-DD --party N`; headed Playwright records every interesting XHR with sensitive header / password / CVC redaction; output `spikes/http_replay/trace.json` gitignored; 7 tests in `test_http_spike_recon.py`)
**Files:** new `spikes/http_replay/recon.py` (gitignored — exploratory only)
**Approach:**
1. Open Tock in headed Playwright; log into the test account.
2. Navigate to the restaurant's search URL with a known date.
3. Record (via `page.on("request")` and `page.on("response")`) every XHR/fetch made during:
   - calendar day click
   - slot button click → checkout transition
   - confirm click
4. Dump request URLs, methods, headers (excluding auth), and response status/shape to `spikes/http_replay/trace.json`.
5. Identify CSRF tokens, anti-CSRF nonces, or `cf-clearance` cookie usage.

**Done when:** we have a complete trace of the booking API path with field/header names documented.

### C.1 — Cookie harvest from Playwright context ✅ Done 2026-05-10 (CLI `python -m spikes.http_replay.harvest`; reads `session_cookies.json`, drops analytics prefixes (`_ga`, `_fbp`, etc.), conservatively keeps unknown Tock-domain cookies, writes `aiohttp_cookies.json` as `{name: value}` dict ready for `aiohttp.ClientSession`; 7 tests in `test_http_spike_harvest.py`)
**Files:** new `spikes/http_replay/harvest.py`
**Approach:** Extract `cf_clearance`, session cookies, and any anti-CSRF token from a logged-in `BrowserContext` and serialize to a format `aiohttp.ClientSession` can consume. Verify against C.0 trace that the cookies are sufficient to authenticate.
**Tests:** can be deferred; spike-mode allowed.

### C.2 — Read-only HTTP availability probe ✅ Done 2026-05-10 — DECISION GATE READY (CLI `python -m spikes.http_replay.probe --url URL`; pure-function `classify_response(status, headers, body) -> Verdict(PASS|BLOCKED|UNCLEAR)`; CF interstitial detection mirrors `_CF_DOM_DETECT_JS`; only ever sends GET — never POSTs; 10 tests in `test_http_spike_probe.py`. **Operator must run end-to-end before C.3 ships.**)
**Files:** new `spikes/http_replay/probe.py`
**Approach:** With harvested cookies, fire `GET <availability_url>` via aiohttp. Compare response to what the bot's checker sees via Playwright. If the data matches and CF doesn't block, **the spike is feasible**.

**Decision gate:** if response is blocked (403, CF challenge, anti-bot), abort the spike — DOM path remains the only option. Document findings in this plan and move on. **No code changes to `src/` if the spike fails.**

### C.3 — Hybrid HTTP booker (only if C.2 succeeds) ⏸ HELD pending operator C.2 result (per the plan: "No code changes to `src/` if the spike fails." Operator must run `recon.py` → `harvest.py` → `probe.py`. If probe returns PASS, request approval before implementing C.3.)
**Files:** new `src/http_booker.py`, modifications to `src/booker.py` and `src/monitor.py`
**Approach:**
- `HttpBooker` wraps the booking POSTs (cart create, confirm).
- Config flag `USE_HTTP_BOOKING: bool = False` (default OFF).
- When True, monitor uses `HttpBooker.book(slot)` instead of `TockBooker.book_best_slot_race()`.
- Hard fallback: any HTTP error → revert to Playwright `TockBooker` for that poll.
- `HttpBooker` MUST honor `dry_run` and `_unverified_confirm_slot` guards.

**Tests:**
- `test_http_booker_dry_run_does_not_post`
- `test_http_booker_respects_uncertain_booking_guard`
- `test_http_booker_returns_failed_on_403`
- `test_http_booker_returns_confirmed_on_success`
- Integration: `--test-http-booking` CLI flag mirroring `--test-booking-flow`, dry_run forced.

**Done when:** in dry-run, HTTP path completes a slot+cart+confirm-prep cycle in <1 s with zero DOM interaction. **Do not flip the flag to True without operator approval and a successful headed test against a real release.**

### C.4 — Telemetry & gradual rollout
**Files:** monitor.py
**Approach:** Once HTTP booker is shippable, log every booking attempt with mode (`http` vs `playwright`) and outcome. After 4 successful HTTP races against real releases, consider making it default.

## Final Step — Codex Adversarial Review

After Phase B1 + B2 + B3 land:
1. Run `pytest tests/ -q` and `compileall src tests`.
2. Dispatch Codex via `codex exec -C <worktree> -s read-only` with an adversarial framing on the diff (use the prior pass prompts as templates).
3. Address HIGH and MEDIUM findings before declaring the phase shippable.

After Phase C lands:
4. **Special focus:** authentication safety, CSRF replay risk, double-booking under HTTP path, fallback correctness when HTTP fails mid-flight.
5. Run `--test-http-booking` against staging restaurant `benu` (per CLAUDE.md test-restaurant pattern) **before** any live release.

## Risks & rollback

| Phase | Risk | Rollback |
|-------|------|----------|
| B1 | Selector batching breaks on UI redesign | Revert specific JS evaluate, restore Python loop |
| B2 | CF detection false-positive blocks legitimate page | Add config flag to disable DOM signal |
| B3.1 | Race fuzz finds a double-booking edge | Restore lock around full _confirm_booking |
| B3.2 | Tock changes XHR shape | DOM fallback already in design |
| B3.3 | Pool exhausts memory | target_size cap |
| C | HTTP path silently double-books | dry-run + uncertain guard + manual flip; never default-on |

## Success metrics

- **Phase B target:** mean detection-to-confirm-clicked latency reduced from ~2 s to <1 s.
- **Phase C target (if feasible):** <500 ms total via raw HTTP.
- **Reliability gate:** 0 increase in failure rate over 4 release windows.
- **Safety gate:** 0 unverified confirms; 0 wrong-time bookings; existing 226 tests still pass at every phase boundary.

## Out of scope

- Migration to a different browser-automation library (Selenium, undetected_chromedriver). Playwright is fine.
- Distributed bot architecture (multiple machines). The race against other bookers is single-actor.
- New restaurant support — Fuhuihua-specific tuning is OK.
