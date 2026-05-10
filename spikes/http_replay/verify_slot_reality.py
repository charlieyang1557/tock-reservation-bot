"""Point-check verification: for each (date, time) slot reported by
either legacy OR replay, navigate directly to that slot's URL and
report what the actual page shows.

A real slot → page shows a "Book" button with the requested time.
A ghost slot → page shows "Notify" / "Sold out" / no time-matching
              button → parser bug, the slot is fictional.

We test slots from both sources to expose:
  - Replay returning real-but-undiscovered slots (legacy was buggy)
  - Replay returning ghosts (replay is buggy)
"""
from __future__ import annotations
import asyncio, sys
from pathlib import Path


# Slots reported by each mode in the cross-mode validation run
LEGACY_SLOTS = [
    ("2026-05-15", "8:30 PM"),
    ("2026-05-23", "8:30 PM"),
    ("2026-05-24", "7:30 PM"),
]
REPLAY_SLOTS = [
    ("2026-05-15", "5:30 PM"),
    ("2026-05-15", "6:30 PM"),
    ("2026-05-15", "7:00 PM"),
    ("2026-05-16", "5:30 PM"),
    ("2026-05-16", "6:30 PM"),
    ("2026-05-22", "5:30 PM"),
    ("2026-05-22", "7:00 PM"),
    ("2026-05-23", "5:30 PM"),
    ("2026-05-24", "6:00 PM"),
]


def _to_24h(t: str) -> str:
    """Convert "5:30 PM" → "17:30"."""
    import re
    m = re.match(r"(\d+):(\d+) (AM|PM)", t)
    if not m:
        return "20:00"
    hh = int(m.group(1)) % 12
    if m.group(3) == "PM":
        hh += 12
    return f"{hh:02d}:{int(m.group(2)):02d}"


async def _check_one(browser, restaurant: str, d: str, t: str) -> dict:
    """Navigate to /restaurant/search?date=D&time=T and report what
    Book/Notify buttons exist + their visible text."""
    page = await browser.new_page()
    try:
        time_24h = _to_24h(t)
        url = f"https://www.exploretock.com/{restaurant}/search?date={d}&size=2&time={time_24h}"
        await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_timeout(2500)  # SPA settles

        # Inspect: find Book/Notify buttons, capture their text
        info = await page.evaluate(
            """
            ({ targetTime12 }) => {
                const out = { book_buttons: [], notify_buttons: [], visible_times: [] };
                const all = Array.from(document.querySelectorAll('button, a'));
                for (const el of all) {
                    const rect = el.getBoundingClientRect();
                    if (rect.width === 0 || rect.height === 0) continue;
                    const text = (el.innerText || el.textContent || '').trim();
                    if (!text) continue;
                    const lower = text.toLowerCase();
                    if (lower.includes('book')) {
                        // Get parent text to see if a time is associated
                        const parent = el.parentElement;
                        const ptext = parent ? (parent.innerText || '').trim() : '';
                        out.book_buttons.push({
                            text: text.slice(0, 60),
                            parent_text: ptext.slice(0, 200),
                            y: Math.round(rect.y),
                        });
                    } else if (lower.includes('notify')) {
                        out.notify_buttons.push({ text: text.slice(0, 40), y: Math.round(rect.y) });
                    }
                }
                // Look for time-shaped strings anywhere visible
                const timeRe = /\\b\\d{1,2}:\\d{2}\\s*(?:AM|PM|am|pm)\\b/g;
                const bodyText = document.body.innerText || '';
                const matches = bodyText.match(timeRe) || [];
                out.visible_times = Array.from(new Set(matches)).slice(0, 30);
                return out;
            }
            """,
            {"targetTime12": t},
        )
        # Determine verdict
        target_in_visible = any(t.replace(" ", "").lower() == m.replace(" ", "").lower() for m in info["visible_times"])
        target_in_book_parent = any(t in (b.get("parent_text") or "") for b in info["book_buttons"])

        verdict = "REAL" if (target_in_visible or target_in_book_parent) else (
            "NOTIFY" if info["notify_buttons"] else "GHOST"
        )
        return {
            "date": d, "time": t, "url": url, "verdict": verdict,
            "n_book": len(info["book_buttons"]), "n_notify": len(info["notify_buttons"]),
            "visible_times_sample": info["visible_times"][:10],
            "book_button_texts": [b["text"] for b in info["book_buttons"][:3]],
        }
    except Exception as e:
        return {"date": d, "time": t, "verdict": "ERROR", "error": str(e)}
    finally:
        try:
            await page.close()
        except Exception:
            pass


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

        print("Verifying LEGACY-reported slots:")
        legacy_results = []
        for d, t in LEGACY_SLOTS:
            r = await _check_one(browser, "benu", d, t)
            legacy_results.append(r)
            verdict = r["verdict"]
            print(
                f"  {d} {t}  → {verdict}  "
                f"(book btns: {r.get('n_book')}, notify: {r.get('n_notify')}, "
                f"visible times: {r.get('visible_times_sample', [])[:5]})"
            )

        print("\nVerifying REPLAY-reported slots:")
        replay_results = []
        for d, t in REPLAY_SLOTS:
            r = await _check_one(browser, "benu", d, t)
            replay_results.append(r)
            verdict = r["verdict"]
            print(
                f"  {d} {t}  → {verdict}  "
                f"(book btns: {r.get('n_book')}, notify: {r.get('n_notify')}, "
                f"visible times: {r.get('visible_times_sample', [])[:5]})"
            )

        # Tally
        def _tally(results):
            from collections import Counter
            c = Counter(r["verdict"] for r in results)
            return dict(c)
        print()
        print("=" * 60)
        print(f"LEGACY tally:  {_tally(legacy_results)}")
        print(f"REPLAY tally:  {_tally(replay_results)}")
        print("=" * 60)

        # Cross-check: for any date both modes mention, list all visible
        # times from a SHARED probe to see what Tock actually offers
        print("\nFor 2026-05-15 specifically — what does Tock show at "
              "the no-time URL?")
        page = await browser.new_page()
        try:
            await page.goto(
                "https://www.exploretock.com/benu/search?date=2026-05-15&size=2",
                wait_until="domcontentloaded", timeout=30000,
            )
            await page.wait_for_timeout(2500)
            # Trigger the calendar to render slots for 5/15
            await page.evaluate(
                """([sel, n]) => {
                    const buttons = document.querySelectorAll(sel);
                    for (const b of buttons) {
                        if (b.textContent.trim() === n) { b.click(); return true; }
                    }
                    return false;
                }""",
                ["button.ConsumerCalendar-day.is-in-month", "15"],
            )
            await page.wait_for_timeout(2000)
            visible_times = await page.evaluate(
                """() => {
                    const re = /\\b\\d{1,2}:\\d{2}\\s*(?:AM|PM|am|pm)\\b/g;
                    return Array.from(new Set((document.body.innerText || '').match(re) || []));
                }"""
            )
            print(f"  Times visible after navigating + clicking day 15:")
            for t in sorted(visible_times):
                print(f"    {t}")
        finally:
            await page.close()

        return 0
    finally:
        await browser.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
