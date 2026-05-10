"""Phase C — Raw HTTP spike for Tock booking.

Operator-runnable scripts to determine whether the bot can replace
Playwright DOM interaction with raw HTTP calls during the
slot-detection-to-confirm-clicked critical path.

Workflow (see README.md):
  1. recon.py — headed Playwright records every XHR during a guided
     booking flow. Output: spikes/http_replay/trace.json
  2. harvest.py — extracts auth cookies (cf_clearance, session) from
     a logged-in browser context to a format aiohttp can consume.
  3. probe.py — fires a GET to a candidate availability URL with
     harvested cookies; reports PASS/BLOCKED/UNCLEAR.

If probe returns PASS, Phase C.3 (HttpBooker) becomes feasible.
If probe returns BLOCKED, the spike fails — no src/ changes ship.
"""
