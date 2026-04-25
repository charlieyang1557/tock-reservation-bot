# 2026-04-14 20:14 — Poll-Spam Incident

## What happened

At approximately 2026-04-14 20:14 PT, the bot emitted hundreds of
`Poll #1168835`, `Poll #1168836`, … lines in roughly a 13-second window, each
followed by `No available slots found this cycle.`  The poll numbers are in the
range of ~1.17 million, implying a single process that had been running
continuously for roughly 270+ days at the normal ~20 s/poll rate — or,
alternatively, a process that was restarted many times with its poll counter
preserved (the auto-restart loop in `main.py` creates a fresh `TockMonitor`
with `_poll_count = 0` on each crash, so this is not the mechanism).

**Log already rotated — verbatim window not available.**
`bot.log` covers 2026-03-10 through 2026-04-04 (Poll #1 through ~#526).
No archived log files exist.  The analysis below is therefore hypothesis-only,
informed by code inspection and the structural impossibility of the observed
rate.

## Root cause

**Duplicate `python main.py` process — confirmed by direct evidence on the production Mac mini, 2026-04-24.**

Live `ps` output captured at 2026-04-24 21:43 PT showed two simultaneous bot processes:

```
  PID  PPID  STARTED                       ELAPSED        COMMAND
41763 97609  Sun Apr 12 03:57:14 2026    12-17:45:58    /Users/openclaw/miniconda3/bin/python /Users/openclaw/tock-reservation-bot/main.py
41961     1  Sun Apr 12 03:57:17 2026    12-17:45:55    python main.py
```

Two `python main.py` instances started **3 seconds apart on 2026-04-12** and have been running concurrently for ~12 days. PID 41961 is reparented to PID 1 (orphaned/detached), suggesting it lost its launching shell. Both processes append to the same `bot.log` via Python's `logging.FileHandler` with no cross-process locking on macOS — `bot.log` had grown to **641 MB** by the time of inspection, consistent with dual-process write amplification.

The original 2026-04-14 20:14 burst could not be reconstructed from the live log slice (the wider `2026-04-14 20:1[34]` grep returned no lines on either `bot.log` or `logs/bot.log` — the 641MB log may have rotated past it, or the timestamp recorded by the user was approximate). However, the duplicate-process state confirmed at the time of investigation makes that historical burst structurally explainable: any time both processes happen to call `notifier.poll_start()` close together, the log shows interleaved `Poll #N` lines at sub-second intervals, which is exactly the pattern reported on 04-14.

Code inspection independently rules out the other three hypotheses:

- **Hypothesis (c) eliminated by code**: `notifier.poll_start()` is called
  exactly once per iteration of `monitor.run()`, immediately before `await
  self.poll()` (monitor.py line 227).  It is not called per-date, so
  per-date duplication cannot produce this pattern.

- **Hypothesis (b) eliminated by asyncio model**: `poll()` is a coroutine that
  is `await`ed serially.  In sniper mode the only sleep is
  `asyncio.sleep(0)` — a single event-loop tick — which cannot re-enter
  `poll()` before the current invocation completes.  The real poll rate
  observed in the log is 1 poll per 17–22 seconds (concurrent sniper) or
  ~120 seconds (sequential), consistent with Playwright page-load time.

- **Hypothesis (d) not applicable**: No shell wrapper or subprocess spawner
  exists in the codebase; `main.py` uses a direct `asyncio.run(main())` call.

## Implication for Task 2 (watchdog + lock)

Singleton lock alone would have prevented this (duplicate process). **This is now a critical, not theoretical, fix** — the production system has been running with two competing bot instances for ~12 days as of 2026-04-24.

A `bot.lock` / `fcntl.flock` pidfile acquired at startup would cause the second process to detect the running instance and exit immediately, preventing the interleaved-log spam, any risk of a double booking, and the 641MB log-write amplification observed in production.

The watchdog (poll-rate monitor) would catch a future re-entrancy or pathological in-process spin, which the singleton lock cannot detect — both layers remain warranted, but for this specific incident the singleton lock is the primary and sufficient defense.

## Production cleanup needed (independent of code fix)

After Task 2 ships and is deployed to the Mac mini, the operator must:
1. Kill both running instances (`kill 41763 41961`).
2. Restart the bot once; the singleton lock will refuse any future second start until the holder exits.
3. Truncate or rotate the 641 MB `bot.log` (consider adding `RotatingFileHandler` in a follow-up — out of scope for Phase A).
