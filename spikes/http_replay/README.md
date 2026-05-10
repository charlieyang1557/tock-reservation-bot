# Phase C — Raw HTTP spike (operator runbook)

This directory contains the operator-runnable scripts for the Phase C
HTTP spike. The goal is to determine whether the bot can replace
Playwright DOM interaction with raw `aiohttp` calls during the
slot-detection-to-confirm-clicked critical path (potential 100–500 ms
saved per booking).

> **Decision gate.** If C.2 (probe) returns `BLOCKED`, the spike is
> over. Per the plan, **no `src/` changes ship.** DOM path remains the
> only option.

## Operator workflow

### 0. Prerequisites

- `pip install aiohttp` (not in requirements yet — only needed for the
  spike).
- The bot has been run at least once so `session_cookies.json` exists
  in the project root.
- A browser session that can pass Cloudflare (run `python main.py
  --once` headed first if cf_clearance has expired).

### 1. C.0 — Recon (≤30 min)

Open a headed Playwright session, navigate to a real (future-dated)
restaurant search page, and click through the booking flow while the
script records every interesting XHR.

```bash
python -m spikes.http_replay.recon \
    --restaurant fui-hui-hua-san-francisco \
    --date 2026-05-23 \
    --party 2
```

In the headed browser:
1. Click the calendar day for the target date
2. Click a time slot button
3. Reach the checkout page
4. **STOP — do NOT click final confirm** unless you actually intend
   to book. The recon flow only cares about the GET path; the POST
   to confirm is for a later step.

Press ENTER in the terminal to write `trace.json`.

Inspect `trace.json`:
- Find the request whose response shape contains the slot list (most
  likely under `/api/consumer/availability/...` or similar).
- Record the URL pattern. This is the candidate URL for C.2.

### 2. C.1 — Cookie harvest (~5 sec)

Extract auth cookies from the bot's session in a format aiohttp
consumes:

```bash
python -m spikes.http_replay.harvest
```

Output: `aiohttp_cookies.json`. **Do NOT commit this file** — it
holds live auth tokens (gitignored).

### 3. C.2 — DECISION GATE: probe (~5 sec)

```bash
python -m spikes.http_replay.probe \
    --url "https://www.exploretock.com/api/consumer/availability/..."
```

Verdicts:

- **PASS** — Tock returned JSON with the harvested cookies. Phase C.3
  (HttpBooker) is feasible. Document the URL in the plan and request
  approval for implementing C.3.
- **BLOCKED** — Tock or CF rejected the request (403, 503, or HTML
  with CF interstitial). **Spike fails.** Document the failure mode
  in the plan's Phase C section. No `src/` changes ship.
- **UNCLEAR** — Inspect manually. Try the URL in a real browser; check
  whether the recon trace's response shape matches what we'd expect
  from raw HTTP.

### 4. (PASS only) C.3 — HttpBooker (NOT YET IMPLEMENTED)

If C.2 returned PASS, file an issue or message the operator with:
- The exact URL pattern that worked
- A sample of the JSON response shape (slot list field name, slot ID
  field, etc.)
- Whether any anti-CSRF tokens or extra headers were required

Then a follow-up commit can implement `src/http_booker.py` per the
plan's C.3 section. **Do not implement C.3 without operator
approval AND a successful C.2 PASS.**

## Files

```
spikes/http_replay/
├── __init__.py
├── README.md          ← this file
├── recon.py           ← C.0 — record XHRs
├── harvest.py         ← C.1 — extract cookies
├── probe.py           ← C.2 — decision-gate probe
├── trace.json         ← gitignored: recon output
└── aiohttp_cookies.json  ← gitignored: harvested auth tokens
```

## Tests

The pure-function pieces of each script are unit-tested:

```bash
python -m pytest tests/test_http_spike_recon.py \
                 tests/test_http_spike_harvest.py \
                 tests/test_http_spike_probe.py -v
```

The CLI integration paths require a real Playwright/aiohttp
environment and are operator-run.

## Safety notes

- `recon.py` runs in `dry_run=True` mode — it never POSTs to confirm
  even if the operator clicks the confirm button.
- `harvest.py` strips analytics cookies but conservatively keeps
  unknown Tock cookies (auth might use an unfamiliar name).
- `recon.py` redacts `Authorization` / `Cookie` / `*-token` request
  headers and `password` / `csrf` / `cvc` body fields from the
  written trace — the trace is NOT a credentials dump.
- `probe.py` only ever sends `GET`. There is no codepath that POSTs.
- All output files are gitignored.
