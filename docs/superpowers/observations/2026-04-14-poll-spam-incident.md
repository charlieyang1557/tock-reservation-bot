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

Root cause not determinable from available logs.

However, the observed pattern — hundreds of `Poll #N` lines in ~13 seconds
accompanied only by `No available slots found this cycle.` — is structurally
impossible from a single legitimate `monitor.run()` loop.  Evidence from code
inspection rules out two of the four hypotheses:

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

- **Hypothesis (a) — duplicate process — is the most consistent explanation**:
  Two simultaneous `python main.py` processes both append to the same
  `bot.log` file via Python's `logging.FileHandler` (no cross-process locking
  on macOS).  Their output would interleave, producing bursts of `Poll #N`
  lines appearing far faster than any single process could generate them.
  The two processes would each have their own `_poll_count` (e.g. Process A
  at poll #1168835 and Process B at poll #N), and their interleaved lines
  would look like sequential but impossibly fast polls from a single source.

  The high poll number (~1.17 million) is consistent with approximately
  272 days of continuous uptime at one poll per 20 seconds.  The bot has
  been running since at least 2026-03-10 (earliest log entry), supporting
  that the production process could have reached that count.

  The most probable trigger: the user manually started a second
  `python main.py` instance (e.g., to test a change) without first killing
  the production process — a scenario that leaves no distinguishing log
  marker because Python's default logger does not include the OS PID.

## Implication for Task 2 (watchdog + lock)

Singleton lock alone would have prevented this (duplicate process).

A `bot.lock` / `fcntl.flock` pidfile acquired at startup would cause the
second process to detect the running instance and exit immediately, preventing
the interleaved-log spam and any risk of a double booking.  The watchdog
(poll-rate monitor) would catch a future re-entrancy or pathological spin, but
for the duplicate-process hypothesis a singleton lock is the primary and
sufficient defense.
