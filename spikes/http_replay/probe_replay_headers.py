"""Phase B3.2 BIG WIN proof — capture SPA's calendar/full request
headers ONCE during initial page load, then replay the fetch with
those exact headers from inside the same browser context.

If this works, we can:
  - Open one page per restaurant
  - Capture the SPA's auth headers on first load
  - Each poll = one in-browser fetch() call with replayed headers
  - No page.reload, no DOM render, no SPA re-init
  - Estimated cycle time: 100–500ms (just the API round-trip)

This would replace the current 12.4s concurrent cycle with a
sub-1s cycle — making the <5s end-to-end booking feasible even
without raw aiohttp (which is CF-blocked).

Usage:
  python -m spikes.http_replay.probe_replay_headers
"""
from __future__ import annotations

import asyncio
import logging
import re
import sys
import time

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
            print("Login failed.", file=sys.stderr)
            return 2

        page = await browser.new_page()

        url = "https://www.exploretock.com/benu/search?date=2026-05-13&size=2&time=17:00"
        print(f"[probe-replay] STEP 1: Navigate to {url} and capture SPA headers")

        captured_request = {}
        async with page.expect_request(
            lambda r: "consumer/calendar/full" in r.url and r.method == "POST",
            timeout=10_000,
        ) as req_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        first_request = await req_info.value
        captured_request["url"] = first_request.url
        captured_request["headers"] = dict(first_request.headers)
        captured_request["post_data"] = first_request.post_data_buffer
        print(f"[probe-replay] Captured {len(captured_request['headers'])} headers + "
              f"{len(captured_request['post_data'] or b'')}-byte body")
        # Print the Tock-specific headers (the ones the SPA injects)
        tock_headers = {k: v for k, v in captured_request["headers"].items() if k.lower().startswith("x-tock-")}
        print("[probe-replay] x-tock-* headers (SPA-injected):")
        for k, v in tock_headers.items():
            print(f"  {k}: {v[:80]}{'…' if len(v) > 80 else ''}")

        # Wait for the SPA to settle so subsequent fetches don't race
        await page.wait_for_timeout(1500)

        # STEP 2: Replay the same fetch from inside the page using
        # captured headers.
        print("\n[probe-replay] STEP 2: Replay fetch() 5× back-to-back with captured headers")

        # Convert post_data bytes to base64 for transport into JS
        import base64
        post_b64 = base64.b64encode(captured_request["post_data"] or b"").decode()

        timings = []
        for i in range(5):
            t0 = time.monotonic()
            result = await page.evaluate(
                """
                async ({ url, headers, postB64 }) => {
                    const t0 = performance.now();
                    const bodyBytes = Uint8Array.from(atob(postB64), c => c.charCodeAt(0));
                    const resp = await fetch(url, {
                        method: 'POST',
                        headers: headers,
                        body: bodyBytes,
                        credentials: 'include',
                    });
                    const buf = await resp.arrayBuffer();
                    const elapsed = performance.now() - t0;
                    const bytes = new Uint8Array(buf);
                    let bin = '';
                    for (let j = 0; j < bytes.length; j++) bin += String.fromCharCode(bytes[j]);
                    return {
                        status: resp.status,
                        body_b64: btoa(bin),
                        body_len: bytes.length,
                        elapsed_ms: Math.round(elapsed),
                    };
                }
                """,
                {
                    "url": captured_request["url"],
                    "headers": captured_request["headers"],
                    "postB64": post_b64,
                },
            )
            wall_ms = (time.monotonic() - t0) * 1000
            body = base64.b64decode(result["body_b64"])
            dates = sorted(set(
                m.group(0).decode()
                for m in re.finditer(rb"20\d{2}-\d{2}-\d{2}", body)
            ))
            times.append(result["elapsed_ms"]) if False else None  # noqa
            timings.append((result["elapsed_ms"], wall_ms, result["status"], result["body_len"], len(dates)))
            print(
                f"  poll {i+1}/5: status={result['status']} body={result['body_len']:>6}b "
                f"dates={len(dates):>3} fetch={result['elapsed_ms']:>4}ms wall={wall_ms:.0f}ms"
            )

        good = [t for t in timings if t[2] == 200 and t[3] >= 1000 and t[4] > 0]
        if not good:
            print(f"\n[probe-replay] BLOCKED — replays failed (got {len(good)}/5 successful)")
            return 1

        avg_fetch = sum(t[0] for t in good) / len(good)
        avg_wall = sum(t[1] for t in good) / len(good)
        print(
            f"\n[probe-replay] PASS — {len(good)}/5 replays returned full calendar data."
        )
        print(f"[probe-replay] Avg fetch latency: {avg_fetch:.0f}ms")
        print(f"[probe-replay] Avg wall-clock per poll: {avg_wall:.0f}ms")
        print(
            f"[probe-replay] vs current sniper cycle: 12.4s "
            f"→ projected speedup: {12400/avg_wall:.0f}x"
        )
        print()
        print("[probe-replay] PATH TO <5s END-TO-END BOOKING:")
        print(f"  detection (replay):  ~{avg_wall:.0f}ms per poll")
        print(f"  + booking tail:      ~3-5s (slot click → checkout → confirm)")
        print(f"  = ~{avg_wall/1000 + 3:.1f}-{avg_wall/1000 + 5:.1f}s end-to-end")
        return 0
    finally:
        await browser.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
