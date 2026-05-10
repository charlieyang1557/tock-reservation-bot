"""Phase C.2 (browser-fetch variant) — bypass aiohttp TLS issues
by firing the calendar/full POST from INSIDE the Playwright browser
context via `page.evaluate("await fetch(...)")`.

aiohttp gets blocked by Cloudflare because its TLS fingerprint
doesn't match a real browser. But fetch() inside the browser uses
Chromium's TLS stack + the active auth cookies (cf_bm, JSESSIONID,
tock_access). This is essentially "ride on the SPA's connection
without going through the SPA."

If THIS works, we have a path to ~1–2s detection cycles (vs 12s
today): one page open → one fetch() per poll → parse protobuf.

Usage:
  python -m spikes.http_replay.probe_browser_fetch
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)


async def _run() -> int:
    from src.browser import TockBrowser
    from src.config import load_config

    cfg = load_config()
    cfg.restaurant_slug = "benu"
    cfg.headless = True

    browser = TockBrowser(cfg)
    await browser.start()
    try:
        if not await browser.login():
            print("Login failed — abort.", file=sys.stderr)
            return 2

        page = await browser.new_page()

        # Warm the page on a Tock URL so the SPA cookies are active.
        # We don't need the SPA to render anything — we just need a
        # same-origin context to fetch() from.
        await page.goto(
            "https://www.exploretock.com/benu/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(1500)  # let initial XHRs settle

        # Use the browser's fetch() to call the calendar API directly.
        # The 4-byte protobuf body Tock sends is "\xda\xba\x1d\x00" =
        # field 27, wire type 2 (length-delimited), length 0 — i.e.
        # an empty request payload. Tock infers the businessId etc.
        # from the page context (x-tock-scope header which the SPA
        # sets via init scripts).
        print("[probe-fetch] Calling fetch('/api/consumer/calendar/full/v2') from page context…")
        t0 = time.monotonic()
        result = await page.evaluate(
            """
            async () => {
                const t0 = performance.now();
                const resp = await fetch(
                    '/api/consumer/calendar/full/v2',
                    {
                        method: 'POST',
                        headers: {
                            'accept': 'application/octet-stream',
                            'content-type': 'application/octet-stream',
                            'x-tock-stream-format': 'proto2',
                        },
                        body: new Uint8Array([0xda, 0xba, 0x1d, 0x00]),
                        credentials: 'include',
                    }
                );
                const buf = await resp.arrayBuffer();
                const elapsed_ms = performance.now() - t0;
                // Convert ArrayBuffer to base64 for transport back to Python.
                const bytes = new Uint8Array(buf);
                let bin = '';
                for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
                return {
                    status: resp.status,
                    content_type: resp.headers.get('content-type'),
                    body_b64: btoa(bin),
                    body_len: bytes.length,
                    elapsed_ms: Math.round(elapsed_ms),
                };
            }
            """
        )
        elapsed_total = time.monotonic() - t0
        print(f"[probe-fetch] Round-trip wall-clock: {elapsed_total*1000:.0f}ms (fetch {result['elapsed_ms']}ms)")
        print(f"[probe-fetch] Status: {result['status']}, Content-Type: {result['content_type']}")
        print(f"[probe-fetch] Body length: {result['body_len']} bytes")

        # Decode the body back to bytes
        import base64
        body = base64.b64decode(result["body_b64"])

        if result["status"] != 200:
            print(f"[probe-fetch] BLOCKED — non-200 status. First 200 bytes:\n{body[:200]!r}")
            return 1

        # Look for date markers in the body — proves it's the calendar payload
        date_pattern = re.compile(rb"20\d{2}-\d{2}-\d{2}")
        time_pattern = re.compile(rb"\d{2}:\d{2}")
        dates = sorted(set(m.group(0).decode() for m in date_pattern.finditer(body)))
        times = sorted(set(m.group(0).decode() for m in time_pattern.finditer(body)))[:20]
        print(f"[probe-fetch] Found {len(dates)} unique date(s) in body: {dates[:10]}")
        print(f"[probe-fetch] Found {len(times)} unique time(s) (first 20): {times}")

        if dates and times:
            print()
            print("[probe-fetch] PASS — browser-fetch path works. Phase C.3")
            print("[probe-fetch] feasibility CONFIRMED via in-browser fetch().")
            print(f"[probe-fetch] Per-call latency: ~{result['elapsed_ms']}ms")
            print("[probe-fetch] This bypasses aiohttp TLS issues entirely.")
            return 0
        print("[probe-fetch] UNCLEAR — body returned but no date markers found")
        return 3
    finally:
        await browser.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
