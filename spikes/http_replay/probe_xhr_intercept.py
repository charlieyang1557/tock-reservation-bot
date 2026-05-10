"""Phase B3.2 fast-path proof — navigate to a search page, INTERCEPT
the calendar/full response that Tock's SPA fires natively, parse the
protobuf body for dates/times.

This is the canonical event-driven design from the plan:
  - SPA does the network work (with all the right headers we can't
    fake from outside)
  - We grab the response BEFORE the DOM finishes rendering
  - Parse dates+times from the protobuf body via regex (no protobuf
    decoder needed — the dates are stored as plain ASCII inside the
    binary frame)

If this works, B3.2 fast-path can replace the slow DOM-render wait
in `_check_date`. Estimated savings: ~0.5–1s per date (the DOM
render delay after the XHR completes), times the cycle factor.

For TRUE big wins, we'd need to skip page.goto entirely — that's
where 80% of the cycle time lives. Possible follow-ups:
  - Use ONE persistent page that calls the SPA's internal refresh
    function (window.tock.refreshCalendar() if it exists)
  - Or do a HEAD-style page load that doesn't render, just fires the
    API call
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

        # Set up the response listener BEFORE navigation so we don't
        # miss the calendar XHR.
        url = "https://www.exploretock.com/benu/search?date=2026-05-13&size=2&time=17:00"

        print(f"[probe-xhr] Setting up response listener + navigating to {url}")
        t0 = time.monotonic()

        async with page.expect_response(
            lambda r: "consumer/calendar/full" in r.url,
            timeout=10_000,
        ) as resp_info:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        response = await resp_info.value
        t_response = time.monotonic() - t0

        body = await response.body()
        t_body = time.monotonic() - t0

        # Parse protobuf body for date + time markers
        dates = sorted(set(
            m.group(0).decode()
            for m in re.finditer(rb"20\d{2}-\d{2}-\d{2}", body)
        ))
        times = sorted(set(
            m.group(0).decode()
            for m in re.finditer(rb"\d{2}:\d{2}", body)
        ))

        print(f"[probe-xhr] Response received at {t_response*1000:.0f}ms (status {response.status}, {len(body)} bytes)")
        print(f"[probe-xhr] Body fully read at  {t_body*1000:.0f}ms")
        print(f"[probe-xhr] Found {len(dates)} unique date(s) in body")
        print(f"[probe-xhr] Found {len(times)} unique time(s) in body")
        print(f"[probe-xhr] Sample dates: {dates[:5]}")
        print(f"[probe-xhr] Sample times: {times[:10]}")

        # Compare to a normal flow that waits for full DOM render
        print()
        print("[probe-xhr] === Comparison: time to ALL slot times via DOM ===")
        page2 = await browser.new_page()
        t0 = time.monotonic()
        await page2.goto(url, wait_until="domcontentloaded", timeout=30_000)
        # Wait for the calendar widget
        await page2.wait_for_selector("div.ConsumerCalendar-month", timeout=10_000)
        # Click day 13 to load slots
        await page2.evaluate(
            """([sel, n]) => {
                const buttons = document.querySelectorAll(sel);
                for (const b of buttons) {
                    if (b.textContent.trim() === n) { b.click(); return true; }
                }
                return false;
            }""",
            ["button.ConsumerCalendar-day.is-in-month", "13"],
        )
        # Wait for slot buttons
        try:
            await page2.wait_for_selector(
                'button:visible:has-text("Book")', timeout=5000
            )
        except Exception:
            pass
        t_dom_done = time.monotonic() - t0
        print(f"[probe-xhr] Time to slots-rendered-in-DOM: {t_dom_done*1000:.0f}ms")
        print()

        if dates and times:
            print(f"[probe-xhr] PASS — XHR intercept gets calendar data in {t_body*1000:.0f}ms")
            print(f"[probe-xhr] vs DOM-wait of {t_dom_done*1000:.0f}ms")
            speedup = t_dom_done / t_body if t_body else 0
            print(f"[probe-xhr] Speedup: {speedup:.1f}x")
            return 0
        return 1
    finally:
        await browser.close()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    return asyncio.run(_run())


if __name__ == "__main__":
    sys.exit(main())
