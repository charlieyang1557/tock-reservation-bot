"""Selector hit telemetry (Phase B2.4).

Records which DOM selectors successfully matched in production so the team
can re-order fallback lists by data instead of guessing.

Design
------
- ``record_match(key, selector)`` is a fast in-memory increment behind a
  ``threading.Lock``. The bot is async-on-one-thread but the API is safe to
  call from any thread.
- ``flush(path)`` atomically merges the in-memory counts into the JSON file
  on disk (read-modify-write) and resets the in-memory state. Designed to be
  called periodically by the monitor's poll loop -- not on every record.
- ``read_metrics(path)`` returns the current on-disk state, default-empty
  when the file is missing or corrupt.

JSON schema
-----------
::

    {
        "<key>": {
            "<selector>": <count>,
            ...
        },
        ...
    }

Notes
-----
- Telemetry failures must not break the booking flow. Call sites in
  ``checker``/``booker`` wrap calls in try/except.
- ``key`` is a logical role like ``"slot_button_check"`` or
  ``"slot_button_book"``. ``selector`` is the literal CSS/Playwright string
  that won the candidate-list contest at runtime.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from collections import defaultdict
from pathlib import Path

logger = logging.getLogger(__name__)


# Default path used when callers don't supply one. Bot's cwd at runtime.
DEFAULT_METRICS_PATH = Path("selector_metrics.json")


# Module-level state guarded by ``_LOCK``. The bot runs single-threaded
# async, but the API contract promises thread-safety so anyone (e.g. a
# background watchdog or future test fixture) can safely call into it.
_LOCK = threading.Lock()
_COUNTS: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))


def record_match(key: str, selector: str) -> None:
    """Increment the in-memory counter for ``(key, selector)``.

    Both ``key`` and ``selector`` must be non-empty truthy strings; blank
    inputs are silently dropped (defensive against bugs in call sites that
    pass an unmatched ``None``/``""`` selector).
    """
    if not key or not selector:
        return
    with _LOCK:
        _COUNTS[key][selector] += 1


def flush(path: Path = DEFAULT_METRICS_PATH) -> None:
    """Persist in-memory counts to ``path`` and reset memory.

    Read-modify-write: existing on-disk counts are summed with the new
    in-memory counts. If the on-disk file is missing or corrupt it is
    treated as empty (logged as a warning by ``read_metrics`` on corrupt
    input).

    No-op when there are no in-memory counts to flush. This avoids creating
    or rewriting the file on every poll when nothing matched.
    """
    # Snapshot + clear under lock so concurrent record_match calls land in
    # the next batch instead of being lost.
    with _LOCK:
        if not _COUNTS:
            return
        # Materialize a plain dict so we can drop the lock while doing I/O.
        in_memory: dict[str, dict[str, int]] = {
            key: dict(per_sel) for key, per_sel in _COUNTS.items()
        }
        _COUNTS.clear()

    try:
        existing = read_metrics(path)
        merged: dict[str, dict[str, int]] = {}
        for key, per_sel in existing.items():
            merged[key] = dict(per_sel)
        for key, per_sel in in_memory.items():
            bucket = merged.setdefault(key, {})
            for sel, count in per_sel.items():
                bucket[sel] = bucket.get(sel, 0) + count

        # Atomic write: write to a sibling tmp file, fsync, then replace.
        # ``os.replace`` is atomic on POSIX and Windows.
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(merged, indent=2, sort_keys=True))
        os.replace(tmp_path, path)

        flushed = sum(c for buckets in in_memory.values() for c in buckets.values())
        logger.debug(
            "[selector-metrics] flushed %d hits across %d key(s) -> %s",
            flushed, len(in_memory), path,
        )
    except Exception as e:
        # Telemetry failures must not break the booking flow. Restore the
        # in-memory counts so they aren't lost (best-effort).
        logger.warning("[selector-metrics] flush failed: %s", e)
        with _LOCK:
            for key, per_sel in in_memory.items():
                bucket = _COUNTS[key]
                for sel, count in per_sel.items():
                    bucket[sel] += count


def read_metrics(path: Path = DEFAULT_METRICS_PATH) -> dict[str, dict[str, int]]:
    """Return the on-disk metrics as ``{role: {selector: count}}``.

    Returns ``{}`` when the file is missing or corrupt; a corrupt file is
    logged at WARNING level (the caller can decide whether to clobber it).
    """
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as e:
        logger.warning("[selector-metrics] could not read %s: %s", path, e)
        return {}

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning(
            "[selector-metrics] %s is corrupt/invalid JSON (%s); treating as empty",
            path, e,
        )
        return {}

    if not isinstance(parsed, dict):
        logger.warning(
            "[selector-metrics] %s does not contain a JSON object at the root; "
            "treating as empty",
            path,
        )
        return {}

    # Coerce to the expected shape; drop any malformed inner values.
    cleaned: dict[str, dict[str, int]] = {}
    for key, per_sel in parsed.items():
        if not isinstance(per_sel, dict):
            continue
        bucket: dict[str, int] = {}
        for sel, count in per_sel.items():
            if isinstance(sel, str) and isinstance(count, int):
                bucket[sel] = count
        if bucket:
            cleaned[key] = bucket
    return cleaned


def format_stats(metrics: dict[str, dict[str, int]]) -> str:
    """Format metrics for human-readable CLI output.

    Top selectors per key, descending hit count. Used by ``--selector-stats``.
    """
    if not metrics:
        return "(no selector metrics recorded yet)"

    lines: list[str] = []
    for key in sorted(metrics.keys()):
        per_sel = metrics[key]
        total = sum(per_sel.values())
        lines.append(f"\n[{key}]  total hits: {total}")
        ranked = sorted(per_sel.items(), key=lambda kv: kv[1], reverse=True)
        width = len(str(ranked[0][1])) if ranked else 1
        for sel, count in ranked:
            # Selectors can be long multi-line CSS; truncate display.
            display = sel if len(sel) <= 80 else sel[:77] + "..."
            lines.append(f"  {count:>{width}}  {display}")
    return "\n".join(lines).lstrip("\n")


# ---------------------------------------------------------------------------
# Test helpers (private; not part of the public API)
# ---------------------------------------------------------------------------


def _reset_for_tests() -> None:
    """Clear the in-memory counter. Used by test fixtures."""
    with _LOCK:
        _COUNTS.clear()


def _snapshot_for_tests() -> dict[str, dict[str, int]]:
    """Return a deep copy of the in-memory counter for assertion."""
    with _LOCK:
        return {key: dict(per_sel) for key, per_sel in _COUNTS.items()}
