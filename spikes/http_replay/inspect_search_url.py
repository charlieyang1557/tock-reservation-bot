"""One-off: navigate to the user's exact URL format on benu, take a
full-page screenshot, list every visible button, dump initial XHRs.

Goal: understand what UI Tock shows for
  /<restaurant>/search?date=YYYY-MM-DD&size=N&time=HH:MM

The user says this URL shows a "pop up calendar" with a Book button
at the bottom — different from what the bot was clicking before.
"""
from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

OUT_SHOT = Path("spikes/http_replay/inspect_search.png")
OUT_BUTTONS = Path("spikes/http_replay/inspect_search_buttons.txt")


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

        # Track interesting XHRs
        xhrs = []
        def on_response(r):
            try:
                if "/api/" in r.url:
                    xhrs.append({"url": r.url, "status": r.status, "method": r.request.method})
            except Exception:
                pass
        page.on("response", on_response)

        # Use the user's exact URL format on benu
        url = "https://www.exploretock.com/benu/search?date=2026-05-15&size=2&time=20%3A00"
        print(f"[inspect] Navigating to {url}")
        await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(3000)  # let SPA settle

        # Take a full-page screenshot
        OUT_SHOT.parent.mkdir(parents=True, exist_ok=True)
        await page.screenshot(path=str(OUT_SHOT), full_page=True)
        print(f"[inspect] Screenshot → {OUT_SHOT}")

        # List every visible button
        buttons = await page.evaluate(
            """
            () => {
                const out = [];
                for (const el of document.querySelectorAll('button, a')) {
                    const rect = el.getBoundingClientRect();
                    const visible = rect.width > 0 && rect.height > 0;
                    if (!visible) continue;
                    const text = (el.innerText || el.textContent || '').trim().slice(0, 60);
                    if (!text) continue;
                    out.push({
                        tag: el.tagName,
                        text: text,
                        cls: (el.className || '').toString().slice(0, 80),
                        x: Math.round(rect.x),
                        y: Math.round(rect.y),
                        w: Math.round(rect.width),
                        h: Math.round(rect.height),
                    });
                }
                return out;
            }
            """
        )
        OUT_BUTTONS.parent.mkdir(parents=True, exist_ok=True)
        with open(OUT_BUTTONS, "w") as f:
            for b in buttons:
                f.write(f"  [{b['tag']:6}] y={b['y']:>5} text={b['text']!r:<60} cls={b['cls']!r}\n")
        print(f"[inspect] {len(buttons)} visible buttons → {OUT_BUTTONS}")

        # Print the most interesting buttons (Book / Reserve / time-looking)
        print("\n[inspect] Buttons containing 'Book', 'Reserve', or a time pattern:")
        for b in buttons:
            t = b["text"]
            if any(k in t for k in ("Book", "Reserve")) or ":" in t and any(c.isdigit() for c in t):
                print(f"  y={b['y']:>5} text={t!r}  cls={b['cls'][:50]!r}")

        # Print the first 15 API XHRs
        print(f"\n[inspect] API XHRs fired during initial load: {len(xhrs)}")
        for x in xhrs[:15]:
            print(f"  {x['method']:5} {x['status']} {x['url'][:120]}")

        return 0
    finally:
        await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
