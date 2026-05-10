"""Phase C.2 (POST variant) — probe Tock's protobuf calendar API.

The base `probe.py` does GETs only. Tock's slot-availability API
turns out to be a POST to `/api/consumer/calendar/full/v2` with a
4-byte protobuf body and `application/octet-stream` content-type.

This script replays that POST using:
  - cookies from `aiohttp_cookies.json` (harvested by harvest.py)
  - Tock-specific headers extracted from the recon trace
  - the same 4-byte request body Tock's SPA sends

If the response is 200 + protobuf body containing date strings, the
HTTP path is feasible (Phase C.3 can ship). If 403/CF-challenge,
the spike fails — we fall back to B3.2 event-driven Playwright.

Decision-gate output: same PASS/BLOCKED/UNCLEAR semantics as
probe.py. Inspects the body bytes for plain-text date markers
(YYYY-MM-DD) since the response is protobuf-encoded but dates
are stored in plain text within the binary frame.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Reuse the same Verdict + classifier from probe.py
from spikes.http_replay.probe import classify_response, Verdict


def _extract_calendar_request_from_trace(trace_path: Path) -> dict | None:
    """Pull the captured calendar/full request from a recon trace so we
    can reuse its custom headers + body verbatim."""
    if not trace_path.exists():
        return None
    data = json.loads(trace_path.read_text())
    for r in data:
        url = r.get("request", {}).get("url", "")
        if "consumer/calendar/full" in url:
            return r["request"]
    return None


async def _probe_post(
    url: str, cookies: dict, headers: dict, body: bytes, timeout_s: float
) -> tuple[int, dict, bytes]:
    try:
        import aiohttp
    except ImportError:
        raise RuntimeError("aiohttp not installed — pip install aiohttp")

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(cookies=cookies, timeout=timeout) as session:
        async with session.post(url, headers=headers, data=body) as resp:
            body_out = await resp.read()
            return resp.status, dict(resp.headers), body_out


def _classify_calendar_response(
    status: int, headers: dict, body: bytes
) -> Verdict:
    """Calendar response is protobuf, not JSON — detect dates in body
    bytes as the PASS signal."""
    base = classify_response(status, headers, body)
    if base.status == "BLOCKED":
        return base
    if status != 200 or not body:
        return base
    # Look for date markers (YYYY-MM-DD) in the body bytes
    if re.search(rb"20\d{2}-\d{2}-\d{2}", body):
        return Verdict("PASS", "HTTP 200 + protobuf body with date markers — endpoint feasible")
    # 200 OK + body but no dates? could be empty calendar — UNCLEAR
    return Verdict(
        "UNCLEAR",
        f"HTTP 200 + {len(body)} bytes but no date markers (YYYY-MM-DD) "
        "in body — operator should inspect manually"
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase C.2 (POST) — probe Tock's protobuf calendar API "
            "using captured headers from a recon trace."
        )
    )
    parser.add_argument(
        "--trace", type=Path,
        default=Path("spikes/http_replay/benu_trace.json"),
        help="Recon trace path (default: spikes/http_replay/benu_trace.json)",
    )
    parser.add_argument(
        "--cookies", type=Path,
        default=Path("spikes/http_replay/aiohttp_cookies.json"),
        help="Harvested cookies (default: spikes/http_replay/aiohttp_cookies.json)",
    )
    parser.add_argument("--timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)

    if not args.trace.exists():
        print(f"[probe-cal] {args.trace} not found. Run auto_recon_benu first.", file=sys.stderr)
        return 2
    if not args.cookies.exists():
        print(f"[probe-cal] {args.cookies} not found. Run harvest first.", file=sys.stderr)
        return 2

    captured = _extract_calendar_request_from_trace(args.trace)
    if captured is None:
        print(
            f"[probe-cal] No `consumer/calendar/full` request found in "
            f"{args.trace}. Re-run recon to capture it.",
            file=sys.stderr,
        )
        return 2

    cookies = json.loads(args.cookies.read_text())

    url = captured["url"]
    # The captured headers are already redacted-safe (recon strips
    # cookie/authorization). For the actual probe we need them BACK —
    # we don't have them in the trace though. Use just the safe headers
    # we DO have; aiohttp will add Cookie automatically.
    safe_headers = dict(captured.get("headers", {}))
    # Keep Tock-specific headers (which are NOT in our redacted list:
    # x-tock-* are app-level metadata, not auth secrets).
    # Remove headers aiohttp doesn't accept verbatim from a captured
    # browser request (e.g., :authority, sec-* fetch metadata).
    for drop in list(safe_headers.keys()):
        if drop.startswith(":") or drop.startswith("sec-fetch-"):
            del safe_headers[drop]

    # The 4-byte protobuf request body Tock's SPA sends to fetch the calendar
    body = bytes.fromhex("daba1d00")
    print(f"[probe-cal] POST {url}")
    print(f"[probe-cal] {len(safe_headers)} captured headers, {len(cookies)} cookies, {len(body)}-byte body")

    status, headers, body_out = asyncio.run(
        _probe_post(url, cookies, safe_headers, body, args.timeout)
    )
    verdict = _classify_calendar_response(status, headers, body_out)

    print()
    print(f"[probe-cal] {verdict.status}: {verdict.reason}")
    print(f"[probe-cal] Status: {status}, Body: {len(body_out)} bytes")
    if body_out:
        print(f"[probe-cal] First 100 bytes: {body_out[:100]!r}")
    print()

    if verdict.status == "PASS":
        print(
            "[probe-cal] DECISION-GATE PASSED. The Tock calendar API\n"
            "[probe-cal] is reachable via raw HTTP with the harvested\n"
            "[probe-cal] cookies. Phase C.3 (HttpBooker) is feasible.\n"
            "[probe-cal] Note: response is protobuf, not JSON — C.3\n"
            "[probe-cal] needs a protobuf decoder OR regex-based date\n"
            "[probe-cal] extraction from body bytes."
        )
        return 0
    if verdict.status == "BLOCKED":
        print(
            "[probe-cal] DECISION-GATE FAILED. Cannot use raw HTTP for\n"
            "[probe-cal] calendar fetch. Fall back to B3.2 event-driven\n"
            "[probe-cal] Playwright (parse Tock's XHR response without\n"
            "[probe-cal] waiting for DOM render)."
        )
        return 1
    print("[probe-cal] DECISION-GATE UNCLEAR. Operator must inspect manually.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
