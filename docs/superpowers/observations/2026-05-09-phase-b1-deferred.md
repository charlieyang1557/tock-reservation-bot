# Phase B1 Deferred Items (2026-05-09)

Codex adversarial review (one pass) on the B1 diff (`1a528b4..93a4a10`)
returned 0 Critical, 3 HIGH, 4 MEDIUM, 2 LOW.

Of those:
- **HIGH 1+2** (PW-selector → querySelectorAll mismatch in checker + booker) —
  fixed in commit `<HEAD+1>`. Locator-loop fallback restored for any
  selector containing `:has-text`, `:text(`, or `:visible`. New tests:
  `tests/test_pw_selector_fallback.py`.
- **MEDIUM 2** (`innerText || textContent` parity) — fixed in same
  commit by swapping to `textContent || innerText` everywhere in the
  batched JS, matching old Playwright `text_content()` behavior.
- **MEDIUM 3** (auth hydration guard) — fixed in same commit by
  replacing the dropped 5 s networkidle wait with a 2 s bounded
  `wait_for_selector(logged_in_indicator)` in `warm_session`. New
  tests added to `tests/test_warm_session_no_networkidle.py`.

The findings below are deliberately deferred to a future phase.

## Deferred — to revisit in Phase B2 / B3 / C planning

### 1. HIGH 3 — Test coverage regression vs. JS algorithm
**Source:** Codex review.

The B1.2 / B1.3 unit tests mock `page.evaluate` rather than running
real JS. This means the JS algorithms (slot click matching + 5-source
extraction) are NOT covered by `pytest`; their correctness is
established by:
- Manual code review of the JS strings
- Integration tests via `python main.py --test-booking-flow`
  (in-browser, hits a live Tock page)

**Why deferred:** real-Playwright unit tests via `page.set_content()`
require a Playwright fixture that opens a Chromium context per test.
That's a significant infrastructure addition (~few hundred lines of
fixture + per-suite Playwright lifecycle). Worth doing before any
further JS edits, but not blocking B1 ship.

**Future improvement:** add `tests/test_js_algorithms.py` using
Playwright's `page.set_content()` to directly exercise
`_CLICK_TIME_SLOT_JS` and `_COLLECT_SLOTS_JS` against fixed HTML
fixtures including:
- exact-time, regex, generic-button-with-parent-time, no-match
- container-scoped vs. page-wide
- aria-label / title fallback
- ancestor-3-deep extraction

### 2. MEDIUM 1 — Selector-string coupling for `is_generic`
**Source:** Codex review (`src/booker.py:68`, `src/booker.py:617`).

`is_generic = matched_selector in _GENERIC_BOOK_SELECTORS` is an
exact frozenset membership check. If the selector string in
`get_slot_button_selectors()` ever drifts (whitespace, quoting,
ordering of the comma-separated `book_now_button`), `is_generic`
silently flips False and the first-button fallback may fire on
a generic restaurant-level button.

**Why deferred:** the existing risk pre-dates B1 — we did not make
it worse. Selectors are touched ≤ once a quarter; a quick selectors
audit would catch drift. Real fix is to attach metadata to selectors
at the `get_slot_button_selectors()` return value (e.g., return
`(selector, kind="generic"|"specific")`).

### 3. MEDIUM 4 — Click-during-navigation false fail
**Source:** Codex review (`src/booker.py:97`, `src/booker.py:629`).

If the button click inside `_CLICK_TIME_SLOT_JS` triggers an
immediate page navigation, Chromium can throw "execution context
destroyed" before the `return { clicked: true, ... }` lands. The
Python wrapper sees the evaluate exception, returns False, and
either falls back (skip-mode) or aborts the booking. But the click
DID land — the booker is now on the wrong code path.

**Why deferred:** needs real-page reproduction to confirm. The old
locator-loop had the same risk (`btn.click()` → navigation →
following Python ops fail) but was less likely to throw because
each step was its own round-trip. May be a non-issue if Tock's slot
click is always intercepted by the SPA before navigation.

**Future improvement:** wrap the JS click in a `try`/`catch` that
returns `{ clicked: true, reason: "exec-context-destroyed" }` instead
of letting evaluate throw. Or set a marker on the button BEFORE
click and inspect it from Python after a small delay if evaluate
throws.

### 4. LOW 1 — Checkout URL match too broad
**Source:** Codex review (`src/booker.py:697`).

`/book` substring matching could false-positive on a future Tock URL
(e.g., `/booklist`). Existing pre-B1 risk; not made worse.

**Future improvement:** require both URL match AND DOM evidence
(`checkout_container` selector) for definitive checkout detection.

### 5. LOW 2 — Payment-visible JS scope
**Source:** Codex review (`src/booker.py:704`).

The `payment_visible_js` predicate matches "Add card / Add payment"
text anywhere on the page. A user's `/account/payment-methods` page
has identical text and would false-trigger.

**Why deferred:** the scenario requires the bot to navigate to
`/account/...` during a booking flow, which it never does.
Defense-in-depth would scope to `checkout_container`.

## Lessons for B2 / B3

1. **Codex caught a real HIGH that pure unit tests missed.** Mocking
   `page.evaluate` lets tests pass while the underlying call would
   throw on real Tock pages. Cross-model adversarial review is
   pulling its weight.
2. **Wrap-the-call vs. test-the-algorithm.** Wrapper tests are cheap
   and verify Python contracts. Algorithm tests are expensive but
   only they catch JS-side correctness. For Phase B2/B3 work that
   adds more JS, pre-invest in the Playwright `page.set_content()`
   fixture.
3. **Stop signal:** one Codex pass found the major issues. A second
   pass would likely find marginal-value items only. Recommended
   to ship after fixing pass 1's HIGH+MEDIUM unless new code is
   added.
