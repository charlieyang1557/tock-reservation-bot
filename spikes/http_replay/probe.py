"""Phase C.2 — DECISION GATE: probe a candidate Tock URL with harvested
cookies and report whether raw-HTTP booking is feasible.

Operator workflow (after running recon.py and harvest.py):
  python -m spikes.http_replay.probe \
      --url "https://www.exploretock.com/api/consumer/availability/?date=..." \
      --cookies spikes/http_replay/aiohttp_cookies.json

Verdict:
  PASS    — fire C.3 (HttpBooker). Tock returned JSON with the
            harvested cookies. Raw HTTP path is feasible.
  BLOCKED — abort the spike. CF or Tock blocked the request.
            Document outcome and STOP — no src/ changes.
  UNCLEAR — operator must investigate manually before proceeding.

The probe makes a SINGLE GET request. It never POSTs (no booking
side effects).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


# Cloudflare interstitial markers — same patterns the bot's checker
# uses (src/checker.py::_CF_DOM_DETECT_JS) for consistency.
_CF_HTML_MARKERS = (
    b"cf-turnstile",
    b"cf-please-wait",
    b"cf-spinner-please-wait",
    b'iframe src="https://challenges.cloudflare.com',
)
_CF_TEXT_PATTERN = re.compile(
    rb"verify you are human|just a moment|checking your browser",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Verdict:
    """Decision-gate output. `status` is one of PASS / BLOCKED / UNCLEAR."""
    status: str
    reason: str


def classify_response(
    status: int, headers: dict[str, str], body: bytes
) -> Verdict:
    """Pure-function classifier of an HTTP response. See module
    docstring for the verdict semantics."""
    content_type = (headers or {}).get("content-type", "").lower()

    # Hard blocks: explicit forbidden / service-unavailable.
    if status == 403:
        return Verdict("BLOCKED", "HTTP 403 Forbidden — Tock or CF rejected the request")
    if status == 503:
        return Verdict("BLOCKED", "HTTP 503 Service Unavailable — likely CF rate-limit")

    # 2xx with JSON + body → PASS
    if 200 <= status < 300:
        if status == 204:
            return Verdict("PASS", "HTTP 204 No Content — endpoint reachable, no body expected")
        if "json" in content_type:
            if body:
                return Verdict("PASS", "HTTP 200 + JSON body — endpoint feasible")
            return Verdict("UNCLEAR", "HTTP 200 + JSON content-type but EMPTY body")
        # 2xx + HTML — check for CF markers first
        if any(marker in body for marker in _CF_HTML_MARKERS):
            return Verdict(
                "BLOCKED",
                "HTTP 200 with HTML containing Cloudflare challenge marker"
            )
        if _CF_TEXT_PATTERN.search(body):
            return Verdict(
                "BLOCKED",
                "HTTP 200 with Cloudflare interstitial text"
            )
        return Verdict(
            "UNCLEAR",
            f"HTTP 200 with non-JSON content-type ({content_type!r}); "
            "operator must inspect manually"
        )

    # All other statuses → UNCLEAR
    return Verdict(
        "UNCLEAR",
        f"HTTP {status} — not a clear PASS or BLOCKED signal"
    )


async def _probe(url: str, cookies: dict[str, str], timeout_s: float) -> Verdict:
    """Fire a single GET. Returns a Verdict."""
    # Lazy import so the module is importable without aiohttp
    try:
        import aiohttp
    except ImportError:
        return Verdict(
            "UNCLEAR",
            "aiohttp not installed — `pip install aiohttp` and re-run"
        )

    headers = {
        "user-agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        ),
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
    }

    timeout = aiohttp.ClientTimeout(total=timeout_s)
    async with aiohttp.ClientSession(cookies=cookies, timeout=timeout) as session:
        async with session.get(url, headers=headers) as resp:
            body = await resp.read()
            return classify_response(resp.status, dict(resp.headers), body)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase C.2 DECISION GATE — probe a candidate Tock URL with "
            "harvested cookies and report PASS/BLOCKED/UNCLEAR. Never "
            "POSTs (no booking side effects)."
        )
    )
    parser.add_argument(
        "--url", required=True,
        help="Candidate slot-availability URL (from recon trace)",
    )
    parser.add_argument(
        "--cookies", type=Path,
        default=Path("spikes/http_replay/aiohttp_cookies.json"),
        help="Path to aiohttp-format cookies JSON (from harvest.py)",
    )
    parser.add_argument(
        "--timeout", type=float, default=10.0,
        help="HTTP timeout in seconds (default 10)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)

    if not args.cookies.exists():
        print(
            f"[probe] {args.cookies} not found. Run "
            "`python -m spikes.http_replay.harvest` first.",
            file=sys.stderr,
        )
        return 2
    try:
        cookies = json.loads(args.cookies.read_text())
    except json.JSONDecodeError as e:
        print(f"[probe] {args.cookies} is not valid JSON: {e}", file=sys.stderr)
        return 2
    if not isinstance(cookies, dict):
        print(
            f"[probe] {args.cookies} must be {{name: value}} dict; got "
            f"{type(cookies).__name__}",
            file=sys.stderr,
        )
        return 2

    verdict = asyncio.run(_probe(args.url, cookies, args.timeout))

    print()
    print(f"[probe] {verdict.status}: {verdict.reason}")
    print(f"[probe] URL: {args.url}")
    print()
    if verdict.status == "PASS":
        print(
            "[probe] DECISION-GATE PASSED. Phase C.3 (HttpBooker) is "
            "feasible. Document the URL pattern in the plan and "
            "request operator approval before implementing C.3."
        )
        return 0
    if verdict.status == "BLOCKED":
        print(
            "[probe] DECISION-GATE FAILED. Spike aborted. Per the plan, "
            "do NOT make src/ changes. Document the failure mode in the "
            "plan's Phase C section and stop."
        )
        return 1
    print(
        "[probe] DECISION-GATE UNCLEAR. Operator must inspect the "
        "response manually (try the URL in a browser; check the recon "
        "trace for the exact request shape Tock sent)."
    )
    return 3


if __name__ == "__main__":
    sys.exit(main())
