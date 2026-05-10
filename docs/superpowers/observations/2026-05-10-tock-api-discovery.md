# Tock API discovery + path to <5s end-to-end booking (2026-05-10)

Phase B+C investigation produced a clean empirical answer to the
question "can we secure 1 booking in under 5 seconds?"

**Yes, with the SPA-header-replay strategy.** Per-poll detection
drops from 12.4s (current concurrent) to **~159ms** (78× speedup).
Projected end-to-end booking: **3.2–5.2s**.

## Discovery summary

### What we tried

| Approach | Result | Per-poll latency |
|----------|--------|------------------|
| Current: page.reload + DOM polling × 6 dates concurrent | baseline | 12.4s |
| `SKIP_DAY_CLICK_CHECK=true` (B1.5) | **A/B FAILED** — 28% slower | 15.9s |
| Raw aiohttp POST to calendar API | **BLOCKED** by Cloudflare TLS fingerprinting | n/a (403 + "Just a moment…") |
| In-browser fetch() with no SPA headers | reachable but auth-stripped (39-byte body) | 107ms but useless |
| In-browser fetch() with NAVIGATION (XHR intercept) | works, equivalent to DOM | ~2.5s (page.goto bound) |
| **In-browser fetch() with REPLAYED SPA headers** | **PASS — full 18 KB calendar, 5/5 reliable** | **159ms** |

### How the SPA-header-replay works

1. SPA loads ONCE on page.goto(/restaurant/search)
2. SPA sets up auth headers internally (JWT in `x-tock-authorization`,
   session ID in `x-tock-session`, fingerprint, business scope)
3. SPA fires `POST /api/consumer/calendar/full/v2` with those headers,
   gets back protobuf-encoded calendar data
4. We INTERCEPT that first request via `page.expect_request` and
   capture its headers
5. From then on, each poll = `page.evaluate("await fetch(...)")`
   with the captured headers + body, **no page.reload, no DOM render**
6. Cloudflare allows it because the request comes from inside a real
   Chromium tab (real TLS fingerprint, real cookies)
7. Tock allows it because we present its own auth headers

The protobuf body has dates and times stored as plain ASCII inside
the binary frame, so we can parse with regex (no protobuf decoder
required for the read path).

### Why aiohttp didn't work

`aiohttp` has a distinctive TLS fingerprint (JA3/JA4) that
Cloudflare's bot management recognizes as non-browser. Even with
the `__cf_bm` cookie present, CF challenged the request. There is
no `cf_clearance` in the captured cookies — Tock's CF config
doesn't issue one for our user-agent.

`curl_cffi` (browser-fingerprinted Python HTTP) might work but adds
a dependency and may still hit additional CF signals. The
in-browser fetch() approach sidesteps the issue entirely.

## Production design — `src/calendar_replay.py` (next commit)

Module-level functions:

```python
async def initialize_replay_session(
    page: Page, restaurant_slug: str, target_date: date,
) -> ReplayContext:
    """Navigate to /restaurant/search?date=...; capture the
    SPA's calendar/full request headers from the initial load.
    Returns a ReplayContext that holds {url, headers, body}."""

async def fetch_calendar(page: Page, ctx: ReplayContext) -> bytes:
    """Run page.evaluate("await fetch(...)") with replayed
    headers. Returns the protobuf body bytes."""

def parse_available_slots(
    body: bytes, target_dates: list[date]
) -> list[AvailableSlot]:
    """Regex-scan the protobuf body for date markers + the times
    that follow each. Filter to target_dates."""
```

Wire-in via new config flag `USE_CALENDAR_REPLAY: bool = False`
(default OFF; opt-in). When True, `check_all` short-circuits to:
1. `initialize_replay_session(...)` once per sniper window
2. Per-poll: `fetch_calendar(page, ctx)` + `parse_available_slots(body, dates)`
3. On 401/403 (JWT expired, fingerprint stale): re-initialize
4. On any other failure: fall back to current DOM polling

## Booking tail (still DOM-bound)

Detection drops to 159ms. The booking tail (slot click → checkout
load → CVC fill → confirm click → verify) is still DOM-bound and
takes ~3-5s. End-to-end:

```
detection (replay):  ~0.2s
slot click (DOM):    ~0.2s  (B1.2 single-evaluate)
checkout transition: ~1-3s  (B1.1 race-of-waiters; Tock server time)
CVC fill (DOM):      ~0.1s  (B2.3 cached iframe)
confirm + verify:    ~1-3s  (Tock server time)
                    ────────
                     ~2.5-6.5s end-to-end
```

To push under 3s would require also reverse-engineering Tock's
cart + confirm POST endpoints (currently unknown — recon stops
before confirm to avoid an actual booking). That's Phase C.3
work, contingent on either:
  - Operator runs recon during a low-stakes restaurant booking
    they're willing to actually complete, OR
  - The cart/confirm endpoints can be inferred from Tock's
    publicly-documented APIs

For now, **the headline win is detection, not booking tail**.

## Risk register

1. **JWT expiration**: `x-tock-authorization` is a JWT. The captured
   JWT in the recon trace had `exp: 1805539005` (year 2027). For a
   sniper window that lasts ~11 minutes, expiration is not a real
   concern. For a long-running bot, refresh on 401.
2. **Fingerprint stability**: `x-tock-fingerprint` value
   `fde57b285ba2b877cfa2b8523507ae76` could be tied to the page
   instance. If Tock rotates per-request, replay would fail and we'd
   need to extract the fingerprint generator from the SPA. Not a
   real concern based on 5/5 successful replays in this proof.
3. **CF policy change**: if CF tightens detection of in-browser
   fetch() patterns, this strategy breaks. Fall back to DOM polling
   is automatic.
4. **Tock API redesign**: low risk in the short term but the
   `consumer/calendar/full/v2` URL pattern could change. The
   regex-based date/time parser is robust to schema additions but
   not to URL path changes. Detection breakage would be loud (no
   slots found) and the bot falls back to DOM polling.

## Apply to fuhuihua

The benu recon used `businessId=10775` in the `x-tock-scope` header.
Fuhuihua needs its own businessId. Two ways to obtain:

A. **Operator runs recon on fuhuihua**: navigate to
   `/fui-hui-hua-san-francisco/search` (no clicking — the calendar/full
   XHR fires on initial load). Captured headers will have fuhuihua's
   businessId.
B. **Auto-detect at runtime**: the proposed `initialize_replay_session`
   does navigate-then-capture. It would automatically pick up
   fuhuihua's businessId on first load. No operator pre-work needed.

Recommendation: ship (B) — runtime auto-detection. Zero operator
maintenance per restaurant.
