# Phase B2 Deferred Items (2026-05-10)

Codex adversarial review (one pass) on the B2 diff (`311b565..d64ace6`,
plus the B1 deferred fixes in `311b565`) returned 0 Critical, 1 HIGH,
3 MEDIUM, 2 LOW.

Of those:
- **HIGH 1** (B2.1 archive-failure unlinks the safety guard) — fixed
  in the follow-up commit. `_archive_uncertain` no longer falls back
  to `unlink`; if archive fails, the live file stays in place and
  `read_uncertain` keeps returning the booking object so future races
  remain blocked.
- **MEDIUM 1** (LOW 1 fix incomplete — regex matched against full URL,
  including query/fragment) — fixed by parsing with `urlparse()` and
  matching against `parsed.path` only. Negative tests cover
  `/search?next=/checkout/abc` and `/account#/book/foo`.
- **MEDIUM 2** (B2.3 iframe cache used substring match) — fixed by
  using exact `urlparse(frame.url).netloc` equality. Eliminates the
  spoof scenario where an attacker iframe at
  `https://attacker.com/spoof/js.stripe.com/...` could be preferred
  over the real Stripe frame.
- **MEDIUM 3** (typed selectors silently default unknown to specific) —
  fixed: unknown selectors now default to `is_generic=True` (the
  safer fail-closed default — refuses first-button fallback) AND log
  a WARNING so the operator knows to add the selector to
  `_SLOT_SELECTOR_ENTRIES` with an explicit kind.

The findings below are deliberately deferred to a future phase.

## Deferred — to revisit in B3 / C planning

### 1. LOW — Cloudflare DOM detection cost
**Source:** Codex review (`src/checker.py:98-110`, `:427-448`).

`_CF_DOM_DETECT_JS` scans `document.querySelectorAll('h1, h2, p, div, span')`
and reads `innerText` per element. `innerText` triggers layout flush
per access; on a heavy Tock redesign with 1000+ DOM nodes this could
add tens of milliseconds per sniper poll.

**Why deferred:** the current production page is small enough that
this isn't a measurable cost. If a future Tock redesign makes pages
heavier, profile via `--test-sniper-benchmark` and switch to:
- Marker selectors first (the iframe/widget query is already there
  and short-circuits)
- Then ONE `document.body.textContent` regex check instead of N
  per-element `innerText` reads
- Or cap candidates to first 50 elements

### 2. LOW — Selector telemetry lossiness on crash
**Source:** Codex review (`src/selector_metrics.py:82-112`,
`src/monitor.py:365-375`).

Telemetry in sniper mode flushes only every Nth poll
(`_SNIPER_METRICS_FLUSH_EVERY_N = 5`). If the bot crashes between
flushes, the most recent 1–4 records are lost.

**Why deferred:** acceptable for best-effort telemetry. The
`--selector-stats` CLI is used for fallback-ordering decisions, not
safety-critical analysis. Document this lossiness in the CLI output
and call it out in the operator's runbook.

**Future improvement:** add a graceful-shutdown hook in
`src/monitor.py` (SIGTERM handler) that calls `selector_metrics.flush()`
before exiting. Drops a class of lost records but doesn't help on hard
crashes (SIGKILL, OOM, kernel panic).

### 3. NOTE — `tests/test_cf_detection_dom.py` weakness vs real Tock
**Source:** Codex review observation.

Like the B1 batched-JS tests, `test_cf_detection_dom.py` mocks
`page.evaluate(_CF_DOM_DETECT_JS)` and asserts the wrapper's
True/False handling — it does NOT exercise the JS algorithm against
real DOM. A Tock redesign that adds a marker class similar to
`.cf-turnstile` (e.g., a payment-confirmation widget) could
false-positive in production while all unit tests still pass.

**Why not fixed now:** same reason as B1's HIGH 3 deferred item —
real-Playwright fixtures via `page.set_content()` are a non-trivial
infrastructure addition. Deferred to B3 / C planning when there's
budget for it.

### 4. NOTE — Deploy-time impact of B2.1 stale-archive
**Source:** Codex observation.

If a stuck `booking_uncertain.json` from a prior bot run sits in the
production checkout when B2.1 deploys, the first `read_uncertain()`
call will:
- Move the file to `booking_uncertain.archive/<ts>_stale__...`
- Return None
- Allow the bot to start racing again

This may or may not be the right thing. If the stuck file represented
a true unverified booking that the operator hadn't verified yet,
auto-archiving silently loses the safety signal.

**Operator runbook addition needed:** before deploying this branch
to the production checkout, manually inspect any existing
`booking_uncertain.json` and verify on Tock that the booking
succeeded or failed. Then archive it manually
(`mv booking_uncertain.json booking_uncertain.archive/manual-pre-b2-deploy.json`).

## Lessons for B3 / C

1. **Codex caught a Pareto-priority HIGH bug we wouldn't have seen.**
   Falling back to `unlink` "to unblock races" was exactly the
   category of well-meaning safety inversion that escapes Claude
   self-review (because we wrote the fallback ourselves with good
   intent).
2. **Substring matching is repeatedly fragile.** Both the URL regex
   (LOW 1 → MEDIUM 1 here) and the iframe cache (MEDIUM 2) used
   substring contains/match patterns that allowed false positives.
   For B3's race-lock-granularity work, double-check any string
   identity comparisons happen via parsed components or strict
   equality.
3. **Test-coverage drift continues.** Both B1 and B2 added new
   `page.evaluate` calls and tested them by mocking the response.
   The category of "real JS does X but mock says Y" is now a known
   gap. B3's spec should explicitly require either real-Playwright
   tests OR a documented decision to defer them again.
