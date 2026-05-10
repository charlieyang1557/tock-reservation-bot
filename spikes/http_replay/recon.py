"""Phase C.0 — Recon: record every interesting XHR during a guided
booking flow.

Operator workflow:
  python -m spikes.http_replay.recon \
      --restaurant fui-hui-hua-san-francisco \
      --date 2026-05-23 \
      --party 2

The script opens a HEADED browser, restores existing session cookies
(or pauses for you to log in if the session expired), navigates to the
restaurant search page, and waits for you to manually click through:
  1. Click the calendar day
  2. Click a time slot button
  3. Reach the checkout page
  4. (Optional) Reach the confirm step — DO NOT actually click confirm
     unless you mean to book

While you click, every XHR/fetch matching the interesting-paths filter
is captured to spikes/http_replay/trace.json.

When done, press ENTER in the terminal to stop recording and write
the trace.

Sensitive data redaction:
  - Authorization, Cookie, and *-token headers are stripped
  - Request bodies containing 'password' fields have the value
    replaced with <redacted>
  - The trace is gitignored regardless

Output:
  spikes/http_replay/trace.json — list of {request, response} entries
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

OUTPUT_PATH = Path("spikes/http_replay/trace.json")

# Substrings that mark a URL as interesting for booking-flow recon.
# Conservative — we'd rather over-record and filter at inspection time.
_INTERESTING_PATTERNS = (
    "/api/",
    "/availability",
    "/cart",
    "/checkout",
    "/reservation",
    "/book",
)

# Substrings that mark a URL as definitely-noise — these get DROPPED
# even if they happened to match an interesting pattern.
_NOISE_HOST_FRAGMENTS = (
    "googletagmanager.com",
    "google-analytics.com",
    "fonts.googleapis.com",
    "fonts.gstatic.com",
    "facebook.com/tr",
    "ingest.sentry.io",
    "cdn.segment.io",
    "doubleclick.net",
    "/static/",
    "/_next/static/",
    "/assets/",
)

# Header keys to redact from the trace. Lowercased.
_SENSITIVE_HEADER_KEYS = (
    "authorization",
    "cookie",
    "set-cookie",
    "x-csrf-token",
    "x-tock-token",
    "x-tock-csrf",
    "x-xsrf-token",
)

# Body fields to redact (case-insensitive substring on field name).
_SENSITIVE_BODY_FIELD_PATTERNS = (
    re.compile(r'"(password|passwd|pwd)"\s*:\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"(token|csrf|nonce)"\s*:\s*"[^"]*"', re.IGNORECASE),
    re.compile(r'"(card_?number|card_?cvc|cvc|cvv)"\s*:\s*"[^"]*"', re.IGNORECASE),
)


def is_interesting_request(url: str) -> bool:
    """Return True if this URL is worth recording for booking-flow recon."""
    if not url:
        return False
    lower = url.lower()
    for noise in _NOISE_HOST_FRAGMENTS:
        if noise in lower:
            return False
    for pat in _INTERESTING_PATTERNS:
        if pat in lower:
            return True
    # Conservative: keep XHRs to exploretock.com that don't match noise.
    if "exploretock.com" in lower and "/static/" not in lower:
        return True
    return False


def _safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """Drop sensitive header keys from a header dict (case-insensitive)."""
    safe = {}
    for k, v in headers.items():
        if k.lower() in _SENSITIVE_HEADER_KEYS:
            continue
        safe[k] = v
    return safe


def _redact_body(body: str | None) -> tuple[str | None, bool]:
    """Replace sensitive field values with <redacted>. Returns
    (redacted_body, was_redacted_flag)."""
    if not body:
        return body, False
    redacted = body
    was_redacted = False
    for pat in _SENSITIVE_BODY_FIELD_PATTERNS:
        new = pat.sub(r'"\1": "<redacted>"', redacted)
        if new != redacted:
            was_redacted = True
            redacted = new
    return redacted, was_redacted


def format_request_record(request: Any) -> dict[str, Any]:
    """Serialize a Playwright Request object to a JSON-safe dict, with
    sensitive headers and body fields redacted."""
    headers = dict(getattr(request, "headers", {}) or {})
    body, body_redacted = _redact_body(getattr(request, "post_data", None))
    record: dict[str, Any] = {
        "url": getattr(request, "url", ""),
        "method": getattr(request, "method", ""),
        "resource_type": getattr(request, "resource_type", ""),
        "headers": _safe_headers(headers),
        "post_data": body,
    }
    if body_redacted:
        record["post_data_redacted"] = True
    return record


def format_response_record(response: Any) -> dict[str, Any]:
    """Serialize a Playwright Response object to a JSON-safe dict."""
    headers = dict(getattr(response, "headers", {}) or {})
    return {
        "url": getattr(response, "url", ""),
        "status": getattr(response, "status", 0),
        "content_type": headers.get("content-type", ""),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _run_recon(
    restaurant: str, date_str: str, party: int, output_path: Path
) -> int:
    """Open headed browser, attach listeners, wait for ENTER, write trace."""
    # Lazy imports so the module is importable for unit tests without
    # Playwright installed.
    from src.browser import TockBrowser
    from src.config import load_config

    cfg = load_config()
    cfg.restaurant_slug = restaurant
    cfg.headless = False  # operator must see the page to click
    cfg.dry_run = True    # do NOT execute a real booking from recon

    browser = TockBrowser(cfg)
    await browser.start()
    try:
        # Make sure session is alive (login if needed)
        login_ok = await browser.login()
        if not login_ok:
            print("Login failed — abort recon.", file=sys.stderr)
            return 2

        page = await browser.new_page()
        url = (
            f"https://www.exploretock.com/{restaurant}/search"
            f"?date={date_str}&size={party}&time=17:00"
        )

        records: list[dict[str, Any]] = []
        # Map request → record so we can attach the response when it
        # arrives. Keyed by id(request) so distinct requests with the
        # same URL stay separate.
        pending: dict[int, dict[str, Any]] = {}

        def on_request(req):
            try:
                if not is_interesting_request(getattr(req, "url", "")):
                    return
                rec = {"request": format_request_record(req), "response": None}
                pending[id(req)] = rec
                records.append(rec)
            except Exception as e:
                logger.debug(f"[recon] on_request error: {e}")

        def on_response(resp):
            try:
                req = getattr(resp, "request", None)
                if req is None:
                    return
                rec = pending.get(id(req))
                if rec is None:
                    return
                rec["response"] = format_response_record(resp)
            except Exception as e:
                logger.debug(f"[recon] on_response error: {e}")

        page.on("request", on_request)
        page.on("response", on_response)

        print(f"\n[recon] Navigating to {url}\n")
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)

        print(
            "[recon] Recording started. Manually click through the booking\n"
            "       flow in the headed browser:\n"
            "         1) Click the target calendar day\n"
            "         2) Click a time slot button\n"
            "         3) Reach the checkout page\n"
            "         4) STOP — do NOT click final confirm\n\n"
            "       Press ENTER in this terminal when done to write the trace."
        )
        # Block until operator presses ENTER. Run in a thread so the
        # asyncio loop can keep dispatching Playwright events.
        await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(records, indent=2))
        print(f"\n[recon] Wrote {len(records)} interesting XHR(s) to {output_path}")
        return 0
    finally:
        await browser.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase C.0 — record interesting XHRs during a guided "
            "Tock booking flow. Operator runs headed and clicks "
            "through the booking flow; this script captures the "
            "underlying API requests to trace.json for inspection."
        )
    )
    parser.add_argument(
        "--restaurant", required=True,
        help="Restaurant slug, e.g. fui-hui-hua-san-francisco",
    )
    parser.add_argument(
        "--date", required=True,
        help="Target date in YYYY-MM-DD format (must be a future date "
             "with available slots)",
    )
    parser.add_argument(
        "--party", type=int, default=2,
        help="Party size (default 2)",
    )
    parser.add_argument(
        "--output", type=Path, default=OUTPUT_PATH,
        help=f"Output trace path (default {OUTPUT_PATH})",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)
    return asyncio.run(
        _run_recon(args.restaurant, args.date, args.party, args.output)
    )


if __name__ == "__main__":
    sys.exit(main())
