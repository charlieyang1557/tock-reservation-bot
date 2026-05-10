"""Phase C.0 — automated recon for benu (no manual clicks).

The original `recon.py` is HEADED + waits for the operator to click
through. That's the right tool for fuhuihua during a real release
window (where the operator must verify what they see).

This script is for INTRODUCTORY discovery on benu, where:
  - benu has reliably-available slots most days (we just confirmed
    via `--test-booking-flow`)
  - We want a TEMPLATE of Tock's XHR/booking API shape that should
    apply to fuhuihua too (same Tock backend)
  - We DO NOT need a human in the loop — the bot can navigate +
    click + reach checkout automatically
  - We MUST stop before the actual confirm POST (no real booking)

The output (`benu_trace.json`) becomes the operator's reference:
  - Identifies the slot-availability XHR URL pattern
  - Captures the JSON response shape
  - Lists every XHR fired during checkout transition
  - Distinguishes safe-to-replay (GET availability) from
    side-effecting (POST cart/confirm)

Usage:
  python -m spikes.http_replay.auto_recon_benu --date YYYY-MM-DD

Both .env and session_cookies.json must exist in cwd (use the same
setup as the bot itself).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path("spikes/http_replay/benu_trace.json")


def _next_weekday(weekday_idx: int, today: date | None = None) -> date:
    """Return the next date with the given weekday (0=Mon..6=Sun)."""
    today = today or date.today()
    delta = (weekday_idx - today.weekday()) % 7
    if delta == 0:
        delta = 7
    return today + timedelta(days=delta)


async def _run(date_str: str, output_path: Path) -> int:
    from src.browser import TockBrowser
    from src.config import load_config
    from spikes.http_replay.recon import (
        is_interesting_request,
        format_request_record,
        format_response_record,
    )

    cfg = load_config()
    cfg.restaurant_slug = "benu"
    cfg.headless = True
    cfg.dry_run = True

    browser = TockBrowser(cfg)
    await browser.start()
    try:
        if not await browser.login():
            print("Login failed — abort.", file=sys.stderr)
            return 2

        page = await browser.new_page()

        # Capture every interesting XHR + map request → response
        records: list[dict[str, Any]] = []
        pending: dict[int, dict[str, Any]] = {}
        bodies: dict[int, asyncio.Future[bytes]] = {}

        def on_request(req):
            try:
                if not is_interesting_request(getattr(req, "url", "")):
                    return
                rec = {
                    "request": format_request_record(req),
                    "response": None,
                    "response_body_sample": None,
                }
                pending[id(req)] = rec
                records.append(rec)
            except Exception as e:
                logger.debug(f"on_request error: {e}")

        async def _read_body_safe(resp) -> bytes:
            try:
                return await resp.body()
            except Exception:
                return b""

        def on_response(resp):
            try:
                req = getattr(resp, "request", None)
                if req is None:
                    return
                rec = pending.get(id(req))
                if rec is None:
                    return
                rec["response"] = format_response_record(resp)
                # Schedule body capture in background; don't block listener
                fut = asyncio.create_task(_read_body_safe(resp))
                bodies[id(req)] = fut
                rec["_body_future_key"] = id(req)
            except Exception as e:
                logger.debug(f"on_response error: {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        url = f"https://www.exploretock.com/benu/search?date={date_str}&size=2&time=17:00"
        print(f"\n[auto-recon] Navigating to {url}\n")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        # Let SPA fully initialize + initial XHRs land
        await page.wait_for_timeout(3000)

        # Click the calendar day. Use the same JS the checker uses.
        target_num = str(int(date_str.split("-")[2]))
        print(f"[auto-recon] Clicking calendar day {target_num}…")
        clicked = await page.evaluate(
            """
            ([selector, targetNum]) => {
                const buttons = document.querySelectorAll(selector);
                for (const btn of buttons) {
                    if (btn.textContent.trim() === targetNum) {
                        btn.click();
                        return true;
                    }
                }
                return false;
            }
            """,
            ["button.ConsumerCalendar-day.is-in-month", target_num],
        )
        if not clicked:
            print(f"[auto-recon] WARN: day {target_num} not in calendar")
        # Wait for the slot-availability XHR + DOM update
        await page.wait_for_timeout(3000)

        # Click the first "Book" button (the slot)
        print("[auto-recon] Clicking first slot Book button…")
        slot_clicked = await page.evaluate(
            """
            () => {
                const sels = [
                    'button.Consumer-resultsListItem.is-available',
                    'button.Consumer-resultsListItem',
                ];
                for (const sel of sels) {
                    const els = document.querySelectorAll(sel);
                    if (els.length > 0) { els[0].click(); return true; }
                }
                // Fallback to generic Book
                const all = document.querySelectorAll('button');
                for (const b of all) {
                    if ((b.textContent || '').trim() === 'Book') {
                        b.click();
                        return true;
                    }
                }
                return false;
            }
            """,
        )
        if not slot_clicked:
            print("[auto-recon] WARN: no slot button found")
        # Wait for checkout transition + cart XHRs
        await page.wait_for_timeout(5000)

        # We do NOT click confirm. Stop here.
        print("[auto-recon] STOPPED before confirm (no booking).")

        # Drain any pending body captures (best-effort, bounded).
        # Snapshot the dict items first — the listener may still be
        # adding entries as page activity quiesces.
        snapshot = list(bodies.items())
        for key, fut in snapshot:
            try:
                body_bytes = await asyncio.wait_for(fut, timeout=2.0)
                # Sample the first 2KB only — full bodies bloat the trace
                # and may hold sensitive content.
                sample = body_bytes[:2048].decode("utf-8", errors="replace")
                # Find the matching record and attach
                for rec in records:
                    if rec.get("_body_future_key") == key:
                        rec["response_body_sample"] = sample
                        rec["response_body_len"] = len(body_bytes)
                        break
            except Exception:
                pass

        # Strip the future-key bookkeeping field from the output
        for rec in records:
            rec.pop("_body_future_key", None)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(records, indent=2))
        print(
            f"\n[auto-recon] Wrote {len(records)} interesting XHR(s) "
            f"to {output_path}"
        )
        return 0
    finally:
        await browser.close()


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    parser = argparse.ArgumentParser(
        description="Automated recon for benu (no human in the loop)"
    )
    # Default: next Wednesday (benu reliably has slots midweek)
    parser.add_argument(
        "--date", type=str,
        default=_next_weekday(2).isoformat(),  # 2 = Wed
        help="Target date YYYY-MM-DD (default: next Wednesday)",
    )
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    return asyncio.run(_run(args.date, args.output))


if __name__ == "__main__":
    sys.exit(main())
