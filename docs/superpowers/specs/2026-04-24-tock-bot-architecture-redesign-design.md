# Tock Bot Architecture Redesign — Design Spec

**Status:** Draft, pending user review
**Date:** 2026-04-24
**Author:** Claude Code session, in collaboration with @charlieyang1557

## Context

Reservation bot for Fuhuihua SF (Tock). Recent post-mortems (Apr 10, Apr 14) revealed two classes of failures:

1. **Tactical bugs** that have been mostly fixed in commits `c412548` … `bf42686` (pre-release degradation, generic-button click, scan-interrupt, etc.).
2. **Architectural limitations** that surface tactical fixes can't address: a single coupled `monitor.poll()` cycle that scans-then-books, no explicit page-state model, no scanner/booker separation, no hybrid HTTP detection path, no defensive infrastructure against runaway loops, and a slot-labeling bug that produces `"Slot N"` strings the booker cannot match against.

Reference materials live in:
- `docs/superpowers/plans/2026-04-10-sniper-critical-fixes.md` — the tactical Apr 10 plan, mostly shipped.
- `docs/superpowers/plans/2026-04-10-speed-and-logic-improvements.md`
- `docs/superpowers/plans/2026-03-28-fix-slot-detection.md`

This spec defines the architectural redesign; it does **not** re-litigate the tactical fixes that already shipped.

## Confirmed scope decisions (from Q&A)

| # | Decision | Rationale |
|---|---|---|
| 1 | **Three sequenced phases**: A (tactical) → B (scanner/booker split) → C (hybrid spike) | Each independently shippable; observation gates between. |
| 2 | **Release pattern: one Friday, time varies, release-detector reliable** | Justifies hot-date pre-positioning. |
| 3 | **Phase C is a 3-day spike** investigating both HTTP-replay and lightweight-headless paths | Turnstile risk makes single-bet too risky; both-paths-then-decide. |
| 4 | **Page pool + strict FSM**, with the predicted release date pre-advanced one extra state | "B as the implementation of C" — coverage of C, hot-date speed of B. |
| 5 | **Strict FSM with validated transitions** (recovery policies deferred) | Sweet spot between diagnosability and over-engineering. |
| 6 | **20:14 log-spam: investigate root cause + add poll-rate watchdog + singleton lock file** | Defense in depth — fix the known bug + prevent the class. |
| 7 | **Sprint-on-demand pacing**, soft target ~1 week Phase A, ~3-4 weeks Phase B, decision by week 6 | Reservation bots are hard to validate without real release windows. |
| 8 | **Tock releases at most the next 2 weeks** of slots | Pool size ~2-3, sniper-mode `scan_weeks=2`. |

---

## Section 1 — Architecture overview & phasing

### Three phases, each independently shippable

```
Phase A  (tactical, ~1 week)
  ├─ Investigate 20:14 log spam, fix root cause
  ├─ Add poll-rate watchdog + singleton lock file
  ├─ Fix slot-labeling problem ("Slot 1" garbage names)
  ├─ Cap scan_weeks=2 inside sniper window
  └─ Tighten container-scoped selectors for slot extraction
              ↓
   Observe 1 Friday release window
              ↓
Phase B  (scanner/booker split, ~2-3 weeks)
  ├─ PagePool: pool of 2-3 PooledPage objects
  ├─ PageStateMachine: strict FSM per page (8 states, validated transitions)
  ├─ Scanner: advances pages PARKED → SLOT_VISIBLE
  ├─ Booker: consumes SLOT_VISIBLE event, advances → CONFIRMED
  ├─ EventBus: scanner emits CANDIDATE_FOUND, booker subscribes
  ├─ Hot-date pre-positioning: predicted Friday parked at DAY_SELECTED before window
  └─ Telemetry: every state transition is a log line w/ timestamp, page id, latency
              ↓
   Observe 1-2 Friday release windows
              ↓
Phase C  (hybrid spike, 3 days investigation + decision)
  ├─ Day 1: HTTP-replay path — capture GraphQL/Turnstile, attempt programmatic replay
  ├─ Day 2: Lightweight-headless path — minimal Playwright probe, asset blocking
  ├─ Day 3: Feasibility report — latency, reliability, eng cost, anti-bot risk
  └─ Decision: build winning path / adopt loser as backup / cancel Phase C entirely
```

### Sequencing rationale

- **A first**: removes correctness bugs that would mask Phase B's performance gains.
- **B next**: it's the architecture every other improvement plugs into. Phase C's HTTP detection becomes a different *Scanner implementation* feeding the same FSM/Booker.
- **C last**: highest reward-to-risk ratio; want stable architecture before attempting the experimental path. If Phase C succeeds, the Booker doesn't change at all; only the Scanner is replaced.

### Pacing (sprint-on-demand with soft targets)

No fixed deadline. Calendar guidance:
- Phase A: ~1 week
- Phase B: ~3-4 weeks after Phase A ships
- Phase C decision: ~6 weeks total

After each phase, write a short "what release window N told us" entry into the spec; that observation gates the next phase. If a Friday release exposes new failure modes, those get folded into the next phase before it's considered done.

---

## Section 2 — Phase A: tactical fixes

Five discrete tasks, each independently testable, all within current architecture. No new abstractions — just bug fixes + defensive infra.

### A1. Investigate the 20:14 log spam (root cause)

**The signal:** `Poll #1168835, #1168836, …` emitted in a ~13s window with `No available slots found this cycle.` on each line. Real polls take ~3-5s minimum; 100s of polls in 13s is structurally impossible from the legitimate poll loop.

**Investigation steps:**
1. Read `bot.log` around 2026-04-14 20:14:00–20:14:30 (a few minutes either side).
2. Verify exact log format — is `Poll #N` printed by `monitor.poll()` (one per real cycle) or by something deeper?
3. Check `notifier.poll_start` / `notifier.no_slots_found` for stray loops.
4. Check whether multiple `python main.py` processes were running (look for two distinct PIDs, or duplicate session-cookie writes).
5. If duplicate process: confirm whether `start-claude.sh` or a launchd/cron config can spawn two copies.
6. Document root cause in spec as a one-line entry; fix lives in A2.

### A2. Poll-rate watchdog + singleton process lock

**Watchdog** (in `monitor.py`):
- Maintain a deque of last 30 poll timestamps.
- On every `poll()` entry: if 10+ entries fall within the last 5s → log `WARNING [monitor] Poll-rate watchdog triggered: N polls in 5s` and `await asyncio.sleep(2)` to break any tight loop. Repeat 3 times → notifier.error and exit non-zero.
- Threshold (`>2 polls/sec`) is well above normal sniper rate (~1 poll per ~3-4s) and well below pathological burst.

**Singleton lock** (in `main.py` startup):
- Acquire `flock()` on `bot.lock` file at startup. If already held by another PID → log holder PID + `lsof bot.lock` output → exit non-zero with clear message.
- Released on clean shutdown via `atexit`. Stale locks (PID no longer alive) are reclaimed automatically.
- Tests: spawn two test processes, second must refuse to start.

### A3. Fix the "Slot 1" labeling bug (Apr 17 root cause)

**The bug** (`checker._collect_slots_multi:793`): when a slot is detected via the generic `button:visible:has-text("Book")` selector and no time can be extracted from button text, parent text, or child span, the fallback is `f"Slot {i + 1}"`. The booker (`_click_time_slot`) then has no real time string to match against, so even with the new generic-button guard, it cannot identify the correct button to click.

**Fix:**
1. Replace the `"Slot {i+1}"` fallback with a **real failure**: if no time can be extracted, log `WARNING` with the matched element's outerHTML (truncated) and *do not emit a slot at all*. A slot the booker can't book is worse than no slot — it fires Discord, derails the booking race, and produces the Apr 17 outcome.
2. Add a fourth extraction source before falling back: walk up to 3 ancestor levels and search each level's text for a time pattern.
3. Add a fifth source: search button's `aria-label` and `title` attributes.
4. Capture an error screenshot (saved to `debug_screenshots/errors/`, never rotated) when extraction fails so you can update selectors with real evidence.

### A4. Cap `scan_weeks=2` inside the sniper window

**Constraint:** Tock releases at most the next 2 weeks. Scanning Friday-3-weeks-out and Friday-4-weeks-out during sniper mode is wasted effort.

**Fix** (in `checker._get_target_dates` / `monitor.poll`):
- Add `Config.sniper_scan_weeks: int = 2` (separate from existing `scan_weeks=4` for normal mode).
- In `check_all`: if `keep_pages` (= sniper mode), cap the date list to dates within `today + sniper_scan_weeks`.
- Outside sniper mode, normal `scan_weeks` still governs.

### A5. Container-scoped slot selectors

**Principle from analysis:** "every click should be scoped to the smallest reliable container."

**Concrete change** (in `selectors.py` and `checker._collect_slots_multi`):
1. Add a new selector key `slots_container` for the wrapping element that holds time-slot buttons (needs DOM inspection in headed mode).
2. `_collect_slots_multi` first finds the container, then runs button selectors *scoped to it*: `container.locator("button…").all()`. A `Book` button outside that container is structurally never a slot button — it can't be a fallback target.
3. If `slots_container` itself can't be found, fall back to current behavior (so we don't regress on restaurants we haven't inspected yet) — but log `WARNING` so we know to update the selector.

### Test coverage for Phase A (TDD per CLAUDE.md)

- `tests/test_log_spam_diagnosis.py` — fixture log file + assert root-cause classification logic (after A1 finds the cause).
- `tests/test_poll_rate_watchdog.py` — feed fake timestamps, assert warning + sleep + exit behavior.
- `tests/test_singleton_lock.py` — two-process test using `subprocess`, assert second exits with the right code/message.
- `tests/test_slot_labeling.py` — mock buttons with various text/aria configurations, assert real time extraction or `None` (never `"Slot N"`).
- `tests/test_sniper_scan_weeks.py` — assert date list is capped at 2 weeks during sniper, full 4 in normal.
- `tests/test_scoped_slot_selectors.py` — mock page with a `Book` button outside the container, assert it's not collected.

Order: A1 → A2 → A3 → A5 → A4 (independent, but A4 is smallest and lowest risk so it can land last).

---

## Section 3 — Phase B: scanner/booker split, page pool, FSM

### Component map

```
            ┌──────────────────────┐
            │      EventBus        │  asyncio.Queue
            │  CANDIDATE_FOUND     │  + simple subscribe()
            │  PAGE_FAILED         │
            │  WINDOW_OPEN/CLOSE   │
            └──────────────────────┘
                    ↑       ↓
   ┌────────────────┴───┐   │
   │      Scanner       │   │
   │  - watches PagePool│   │
   │  - advances pages  │   │
   │    PARKED →        │   │
   │    SLOT_VISIBLE    │   │
   │  - emits events    │   │
   └─────────┬──────────┘   │
             │              │
             ↓              ↓
        ┌─────────────────────────┐
        │      PagePool           │
        │  ┌─────┐ ┌─────┐ ┌─────┐│   2-3 pages
        │  │P-Hot│ │P-2  │ │P-3  ││   one is "hot" (predicted date)
        │  │FSM  │ │FSM  │ │FSM  ││   each owns a PageStateMachine
        │  └─────┘ └─────┘ └─────┘│
        └─────────────────────────┘
                    ↑
                    │  takes SLOT_VISIBLE page
        ┌───────────┴────────────┐
        │       Booker           │
        │  - subscribes to       │
        │    CANDIDATE_FOUND     │
        │  - claims page,        │
        │    advances FSM        │
        │    SLOT_VISIBLE →      │
        │    CONFIRMED           │
        └────────────────────────┘
```

### B1. `PageStateMachine` (per page, strict)

8 states, declared transitions, async actions:

```
PARKED                 # blank tab, no nav yet
   ↓ goto(restaurant)
RESTAURANT_LOADED      # main page, cookies fresh
   ↓ goto(search?date=X)
CALENDAR_LOADED        # calendar widget rendered, no day clicked
   ↓ click_day(X)
DAY_SELECTED           # day click fired, awaiting slot DOM
   ↓ slot buttons appear (auto)
SLOT_VISIBLE           # ≥1 bookable slot detected — booker territory
   ↓ click_slot(time)
CHECKOUT_LOADING       # post-slot-click navigation in flight
   ↓ checkout DOM detected
CHECKOUT_READY         # confirm button visible
   ↓ click_confirm()
CONFIRMED              # terminal

   FAILED              # terminal — recoverable by reset to PARKED via PagePool
```

**Implementation shape:**

```python
class PageStateMachine:
    _ALLOWED = {
        PageState.PARKED:           {PageState.RESTAURANT_LOADED, PageState.FAILED},
        PageState.RESTAURANT_LOADED:{PageState.CALENDAR_LOADED, PageState.FAILED},
        PageState.CALENDAR_LOADED:  {PageState.DAY_SELECTED, PageState.FAILED},
        PageState.DAY_SELECTED:     {PageState.SLOT_VISIBLE, PageState.FAILED},
        PageState.SLOT_VISIBLE:     {PageState.CHECKOUT_LOADING, PageState.FAILED},
        PageState.CHECKOUT_LOADING: {PageState.CHECKOUT_READY, PageState.FAILED},
        PageState.CHECKOUT_READY:   {PageState.CONFIRMED, PageState.FAILED},
    }

    async def advance_to_calendar_loaded(self, date: date) -> None: ...
    async def advance_to_day_selected(self) -> None: ...
    async def advance_to_slot_visible(self, timeout_ms: int) -> bool: ...
    async def advance_to_checkout_loading(self, slot: AvailableSlot) -> None: ...
    async def advance_to_checkout_ready(self) -> bool: ...
    async def advance_to_confirmed(self) -> bool: ...

    def _transition(self, new_state: PageState) -> None:
        if new_state not in self._ALLOWED[self._state]:
            raise IllegalTransition(self._state, new_state)
        prev, self._state = self._state, new_state
        self._emit_telemetry(prev, new_state)  # auto log+timestamp+latency
```

Every illegal transition raises immediately — Apr 17-style "click stuff and hope" becomes structurally impossible. Telemetry emission is automatic per transition.

### B2. `PooledPage` + `PagePool`

```python
@dataclass
class PooledPage:
    id: str                    # "hot", "n+1", "n+2"  (stable ID for logs)
    target_date: date | None   # what date this page is positioned for
    sm: PageStateMachine       # the FSM
    is_hot: bool = False       # True = pre-advanced one extra state pre-window
```

`PagePool` responsibilities:
- Own all `PooledPage` instances (default 2-3, configurable).
- `async def warm_up(predicted_date: date, neighbor_dates: list[date])`: positions every page at the right state for its date. Hot page gets `DAY_SELECTED`; others get `CALENDAR_LOADED`.
- `async def reset(page_id)`: drop a failed page back to `PARKED`, recreate Playwright page if needed. Used when an FSM transitions to `FAILED`.
- `def find_winner() -> PooledPage | None`: return any page in `SLOT_VISIBLE` state; called by booker.
- `async def shutdown()`: close all pages.

### B3. `Scanner` (replaces most of `checker.py`)

Purpose-built for the new model. Doesn't care about booking, doesn't own the date list logic — just watches/advances pages.

```python
class Scanner:
    def __init__(self, pool: PagePool, bus: EventBus, config: Config):
        ...

    async def run(self, scan_dates: list[date]) -> None:
        """Main scanner loop — runs while window is open."""
        while self._running:
            tasks = [self._tick_page(p) for p in self.pool.pages()]
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(self._tick_interval)

    async def _tick_page(self, page: PooledPage) -> None:
        match page.sm.state:
            case PageState.CALENDAR_LOADED:
                await page.sm.advance_to_day_selected()
            case PageState.DAY_SELECTED:
                if await page.sm.advance_to_slot_visible(timeout_ms=500):
                    await self.bus.emit(CandidateFound(page.id, page.target_date))
            case PageState.SLOT_VISIBLE:
                pass  # waiting for booker to pick it up
            case PageState.FAILED:
                await self.pool.reset(page.id)
```

Each tick is non-blocking (~500ms max per page). The full scan tick across the pool is ~1.5s worst case.

### B4. `Booker` (refactored from `booker.py`)

Subscribes to `CANDIDATE_FOUND`, claims the page, drives FSM through checkout.

```python
class Booker:
    async def run(self) -> None:
        async for event in self.bus.subscribe(CandidateFound):
            page = self.pool.try_claim(event.page_id)
            if page is None:
                continue  # already claimed by another handler
            success = await self._book(page)
            if success:
                await self.bus.emit(BookingConfirmed(page.target_date))
                return  # booker exits — we have a reservation

    async def _book(self, page: PooledPage) -> bool:
        try:
            await page.sm.advance_to_checkout_loading(self._pick_best_slot(page))
            if not await page.sm.advance_to_checkout_ready():
                return False
            return await page.sm.advance_to_confirmed()
        except IllegalTransition as e:
            logger.error(f"[book] FSM violation: {e}")
            await self.pool.reset(page.id)
            return False
```

### B5. `EventBus` (tiny)

`asyncio.Queue`-backed pub/sub. ~30 LOC. Events are dataclasses (`CandidateFound`, `PageFailed`, `WindowOpen`, `WindowClose`, `BookingConfirmed`). Subscribers are async generators. No external dependency.

### What gets retired vs thinned

| Today | After Phase B |
|---|---|
| `checker.py` (833 LOC) | thinned to ~200 LOC: only date-list builder and Phase 1 (preferred) / Phase 2 (fallback) routing logic. The actual page-driving moves to Scanner + PageStateMachine. |
| `booker.py` (674 LOC) | thinned to ~250 LOC: payment helpers (`_fill_cvc`, `_has_saved_card`, `_page_needs_payment`) stay; race logic, click-time-slot, click-day, wait-for-checkout all move into PageStateMachine actions. |
| `monitor.py` poll loop | restructured to: pre-warm → spin up Scanner+Booker → wait for `BookingConfirmed` or window close → tear down. Adaptive concurrent/sequential switching becomes a *Scanner tick-rate adjustment* rather than a mode flag. |

### Hot-date pre-positioning (the B-as-implementation-of-C bit)

In `monitor.py`'s pre-warm phase (15 min before window):
1. Call `release_detector.detect_release_time()` for the predicted date.
2. `pool.warm_up(predicted_date=X, neighbor_dates=[X-7, X+7])`.
3. PagePool advances `P-Hot` all the way to `DAY_SELECTED` and parks it there. Other pages stop at `CALENDAR_LOADED`.
4. When the release fires, `P-Hot`'s next tick advances directly to `SLOT_VISIBLE` — no day click needed, ~500ms-1s critical path savings.

If release-detector is wrong, `P-Hot` is just one of three pages racing — no harm done, you fall back to C-style uniform behavior automatically.

### Test strategy for Phase B

- **Unit:** every FSM transition in isolation (mock Page). Every illegal transition raises. PagePool.warm_up positions correctly. EventBus FIFO + multi-subscriber.
- **Integration:** Scanner+Booker+PagePool against a Playwright fixture serving a static HTML mock of the Tock search page. Validates the full happy path + 4 failure modes (page CF-blocked, slot disappears mid-handoff, payment missing, FSM illegal-transition raised).
- **End-to-end (manual):** A new `--test-fsm-pipeline` flag mirroring the existing test flag pattern, drives the full pipeline against a non-Fuhuihua restaurant (Benu) with `dry_run=True`.

### Cutover strategy

Phase B is a refactor of the hot path. Risk of breaking the bot mid-redesign is real. Mitigation:
- Build new components alongside the old ones (`src/scanner.py`, `src/booker_v2.py`, `src/page_pool.py`).
- New components opt-in via `Config.use_v2_pipeline: bool = False`.
- Once tests + 1 dry-run release window pass, flip the flag to True.
- After 2 successful release windows on V2, delete the old code paths.

### Telemetry deliverables

Every FSM transition emits a structured log line:
```
[fsm] page=hot date=2026-04-24 PARKED→RESTAURANT_LOADED dur=842ms
[fsm] page=hot date=2026-04-24 RESTAURANT_LOADED→CALENDAR_LOADED dur=1240ms
[fsm] page=hot date=2026-04-24 CALENDAR_LOADED→DAY_SELECTED dur=180ms
```

Plus screenshot triggers: any `→FAILED` transition automatically saves a screenshot to `debug_screenshots/errors/fsm_failed_<page_id>_<from_state>_<ts>.png`. Replaces the ad-hoc screenshot calls scattered across `booker.py` today.

---

## Section 4 — Phase C: hybrid spike

A 3-day **investigation**, not a build. Output is a feasibility report and a build/no-build decision.

### Goal

Determine whether the per-date "is this slot open?" probe can be made dramatically faster than today's full-page Playwright load (~1-3s/date), while surviving Tock's Cloudflare Turnstile protection.

### Day 1 — HTTP-replay path

**Hypothesis:** Tock's GraphQL endpoint can be called directly using cookies harvested from a live Playwright session. Latency target: ≤200ms/date.

**Steps:**
1. Run the bot in headed mode against Fuhuihua. Open Chrome DevTools → Network → filter by `graphql`.
2. Click around: change date in calendar, click an available slot, observe the request waterfall.
3. Capture the request that returns slot availability. Document: URL, method, headers (`cf_clearance`, `__cf_bm`, `x-tock-csrf`, `x-tock-client`), GraphQL operation name, query/variables shape.
4. Capture the response shape — what JSON tells you a slot is bookable vs sold out vs not-yet-released.
5. Replay the captured request from `aiohttp` using cookies extracted via `browser.get_cookies()`. Measure: does Turnstile block the first call? After how many calls does it block?
6. Test cookie freshness: how stale can `cf_clearance` get before Turnstile rejects?
7. Test rate limit: how many requests per second before something breaks?

**Decision criteria for HTTP path = viable:**
- ≥10 successful probes in a row, no Turnstile challenge.
- p95 latency ≤300ms (full request + parse).
- Cookies harvested from a Playwright session remain valid for ≥10 min of probing.
- Documented graceful-degradation path: when Turnstile *does* challenge, fall back to a Playwright probe (use the existing FSM scanner) without losing the window.

**Decision criteria for HTTP path = unviable:**
- Any of: Turnstile challenges within first 3 probes, requires JS execution to compute a header, requires a per-request token that only the page's JS can mint.

### Day 2 — Lightweight headless probe path

**Hypothesis:** A stripped-down Playwright page with aggressive resource blocking and shared browser context can hit per-probe latency of ~300-500ms.

**Steps:**
1. Build a probe page that:
   - Reuses the existing `BrowserContext` (don't pay context-creation cost).
   - Routes & blocks: images, fonts, CSS, analytics, third-party tracking, all `*.gif`/`*.woff`/`*.png`. Keep only HTML, JS, JSON XHR.
   - Skips waiting for `domcontentloaded` — use `load` only for the initial HTML, then `wait_for_function` on a slot-specific JS condition.
   - Uses `page.evaluate()` to read slot state directly from window state / DOM in a single call.
2. Probe the same date 30 times in sequence. Measure: p50, p95, p99 latency. Cloudflare error rate.
3. Probe 3 dates concurrently using `BrowserContext.new_page()` × 3 sharing one context. Measure same metrics.
4. Test Turnstile resistance: does asset blocking break Turnstile challenge?
5. Compare against today's baseline: capture 30 probes from current `checker._check_date()` for the same date. Compute speedup.

**Decision criteria for lightweight-headless = viable:**
- p95 ≤600ms per probe.
- ≥3x speedup over current baseline.
- ≤5% Cloudflare/Turnstile error rate over 100 probes.

### Day 3 — Feasibility report + decision

**Output document** (`docs/superpowers/specs/<date>-phase-c-feasibility.md`):

| Section | Content |
|---|---|
| Latency table | HTTP p50/p95/p99, Headless p50/p95/p99, current baseline. |
| Reliability table | Turnstile challenge rate, error recovery time, cookie staleness behavior. |
| Eng cost estimate | Days to production-ready for each path. |
| Anti-bot risk | What Tock could deploy that would break each path; how brittle each is. |
| Recommendation | One of: build HTTP / build Headless / hybrid (HTTP + headless fallback) / cancel Phase C. |

**Decision tree:**

```
HTTP viable?
├── Yes → HTTP-only, headless as fallback when Turnstile rejects
│         (best case: ~150ms probes with degradation path)
│
├── No, but Headless viable → Build lightweight headless Scanner
│                              (good case: ~400ms probes, much more reliable)
│
├── No to both → Cancel Phase C; double down on Phase B optimizations:
│                · request interception in current pages (block fonts/images)
│                · HTTP/2 connection reuse via shared context
│                · finer-grained FSM tick rate
│
└── HTTP works but unstable → Adaptive: HTTP for normal polling,
                              headless for sniper window (where reliability matters more)
```

### Integration with Phase B

Phase C only adds (or doesn't add) a **new Scanner implementation**. The Booker, PagePool, FSM, EventBus all stay unchanged. This is the leverage of Phase B — Phase C becomes a swap-in, not a parallel architecture.

If Phase C builds the HTTP path:
- New file: `src/http_scanner.py` — implements the same Scanner interface as `src/scanner.py`.
- Subscribes to the same EventBus, emits the same `CandidateFound` events.
- On HTTP success: skips PagePool entirely for detection (much faster), but on `CandidateFound` the booker still uses the warm pool to convert to checkout — because the *checkout* leg can't be HTTP'd (it requires interactive payment confirmation).

If Phase C builds the Headless path:
- New file: `src/lightweight_scanner.py` — same interface.
- Manages its own pool of probe-only pages separate from the booker pool, since they have different lifecycles.

If Phase C is cancelled:
- The investigation report remains in the spec as documentation of *why* — useful when the question comes up again in 6 months.
- Phase B improvements (request interception, asset blocking on existing pages) get pulled into a small follow-up sprint.

### Test/spike rigor

The spike is exploratory but not undisciplined:
- All probe runs scripted (`scripts/spike_http.py`, `scripts/spike_headless.py`) so results are reproducible.
- Raw measurements committed to `docs/superpowers/specs/data/` for future reference.
- Spike code is not production-quality; clearly marked `# SPIKE — not for production`.

---

## Section 5 — Cross-cutting concerns

### Testing strategy across phases

| Phase | Unit | Integration | E2E (manual) |
|---|---|---|---|
| **A** | Each fix has its own test file (per CLAUDE.md TDD rule). All five fixes ship with ≥80% coverage on changed lines. | Existing `--test-sniper-phases` and `--test-adaptive-sniper` flags continue to pass. New test: `--test-singleton-lock` verifies two-process refusal. | One headed dry-run against Benu before merge; one observed Friday release window before declaring Phase A done. |
| **B** | FSM transitions tested in isolation with mock `Page`. PagePool warm-up state correctness. EventBus FIFO + multi-subscriber. Scanner tick logic per state. Booker claim race (two events for same page → exactly one wins). | New flag `--test-fsm-pipeline` runs full Scanner+Booker+PagePool against a static-HTML mock served by a local aiohttp fixture. Validates happy path + 4 failure modes. | Headed dry-run against Benu with V2 pipeline flag on; observe two Friday windows on V1 in parallel as control before flipping. |
| **C** | Spike scripts have light asserts (latency budget, error-rate budget). No production-quality test coverage on spike code. | If Phase C builds: new Scanner gets the same `--test-fsm-pipeline` integration test, same fixtures. | One Friday release window observation before declaring Phase C done. |

The TDD rule from CLAUDE.md (`Write failing tests BEFORE implementation`) holds for all of A and B. Spike code is the only exception and is marked as such.

### Observability deliverables

**Phase A** — fixes the noise problem:
- Watchdog warning when poll-rate is anomalous.
- Singleton-lock startup log line: `[startup] Acquired bot.lock (PID=12345)`.
- Real slot times in logs instead of `Slot 1`.

**Phase B** — adds structured state telemetry:
- One log line per FSM transition (`[fsm] page=hot date=… A→B dur=Nms`).
- Auto-screenshot on `→FAILED` (errors/ folder, never rotated).
- New Discord embed: `Window summary` posted when window closes, listing every state transition latency p50/p95 across pages — turns each release window into a data point.

**Phase C** — adds latency comparisons:
- Per-probe latency histogram in spike report.
- If Phase C ships: a per-probe log emitter so we can compare HTTP vs Playwright performance under real Friday load.

### Risk register

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| Phase B refactor breaks production mid-release | Medium | High — could miss a Friday window | V1/V2 toggle (`Config.use_v2_pipeline`); flip only after dry-run + 1 silent parallel observation. Don't flip on the day of a release. |
| FSM is over-engineered for actual failure modes | Low-Med | Med — wasted weeks | Recovery policies deferred to Phase B+1 (already decided). Strict-FSM-only minimizes upfront ceremony. |
| Hot-date prediction is wrong → P-Hot wasted | Medium | Low | P-Hot is just one of N pages; if wrong, you lose ~1s on the actual hot date but other pages still scan it. Worst-case = uniform-C performance, which is still better than today. |
| Turnstile defeats both Phase C paths | Medium | Low — Phase C cancels gracefully | Phase C is exploratory; cancellation is a documented outcome. Phase B alone is a ~2-3x improvement over today. |
| Tock changes selectors mid-implementation | Low (recently) | High | Existing `--verify` flag + selectors-in-one-file convention is preserved. FSM transitions log the exact selector that failed when they fail. |
| Two release windows happen during V2 cutover and both miss | Low | High | The `Config.use_v2_pipeline` flag has an emergency-rollback path: a single env var change, restart, back to V1. Document the rollback procedure in the spec. |
| Phase A's slot-labeling fix accidentally drops legitimate slots | Low | High — silent miss | Add a `WARNING` log + error-screenshot when extraction fails, so any false negative is loud. Run two release windows on Phase A before declaring complete. |
| 20:14-style log spam recurs after watchdog | Low | Med | Watchdog escalates: warn once, sleep, warn twice, exit. Worst case is the bot kills itself instead of running hot — recoverable via systemd-style auto-restart if you run one. |

### What we are explicitly NOT doing (YAGNI)

- **Multi-restaurant support** — code stays single-restaurant. PagePool is sized for Fuhuihua's pattern.
- **Recovery policies per FSM state** — deferred to Phase B+1, only built if production data shows a state failing repeatedly.
- **Removing `release_detector.py`** — it works and is reliable; integrates cleanly with hot-date pre-positioning.
- **Discord-channel allowlist changes** — the existing memory rule (DM, #general, #fuihuahua-bot) is preserved unchanged.
- **Rust/C++ rewrite** — explicitly off the table per the analysis.
- **Cloudflare-bypass research beyond Phase C spike** — if Phase C concludes Turnstile is impassable, we don't pursue further bypass work.
- **Multi-page-per-date pre-positioning beyond hot-date** — one hot page is enough; investing in deeper pre-position state for cold pages has diminishing returns and complicates FSM.
- **Replacing Discord with another notification channel.**

### Success metrics (used to gate phase advancement)

| Phase | Success means… |
|---|---|
| A | (1) No `Slot N` label appears in any production log for 1 release window. (2) Singleton lock + watchdog observed firing in test, dormant in production. (3) `--verify` still passes. (4) Apr 14-style log burst does not recur in 2 release windows post-A. |
| B | (1) Full FSM pipeline books a Benu test reservation in dry-run, end-to-end. (2) Telemetry shows ≤5s detection→checkout latency on the Benu test. (3) After flag flip: V2 captures a real Fuhuihua reservation in 1 of 2 attempts (acknowledging release competition is luck-dependent). (4) Zero V1/V2 cross-talk bugs (no double-booking, no orphan pages). |
| C | (1) Feasibility report committed and reviewed. (2) Recommendation is unambiguous (build / cancel / hybrid). (3) If build: spike code archived; production code lives in `src/http_scanner.py` or `src/lightweight_scanner.py`. (4) If cancel: at least 2 small Phase B optimizations identified and prioritized as a follow-up sprint. |

### Documentation deliverables

- **Spec doc:** this file.
- **Phase A plan:** `docs/superpowers/plans/2026-04-24-phase-a-tactical-fixes.md` (produced by writing-plans skill after spec approval).
- **Phase B plan:** `docs/superpowers/plans/<date>-phase-b-scanner-booker-split.md` (after Phase A ships and observes 1 release window).
- **Phase C feasibility report:** `docs/superpowers/specs/<date>-phase-c-feasibility.md` (output of the spike).
- **Release-window observation log:** `docs/superpowers/observations/<date>-window-N.md` — short notes after each Friday window: what happened, what surprised us, what the next phase should address.
- **CLAUDE.md update** — after Phase B ships, document the new module map (Scanner / Booker / PagePool / EventBus / FSM) replacing the current checker/booker/monitor description.

---

## Approval

- [ ] User has reviewed this spec
- [ ] Hand off to `superpowers:writing-plans` skill to produce the Phase A implementation plan
