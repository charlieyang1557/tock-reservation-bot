"""Phase B3.2 — first pass: XHR telemetry recorder.

When `config.event_driven_detection` is True, the checker creates an
`XhrTelemetryRecorder` per `_check_date` call. The recorder registers
a Playwright `response` listener during the day-click that records
every matching XHR to `xhr_telemetry.jsonl` for operator analysis.

Operator workflow (per docs/superpowers/plans/2026-05-09-booking-speedups.md):
  1. Set `EVENT_DRIVEN_DETECTION=true` in .env
  2. Run a real release-window cycle
  3. Inspect `xhr_telemetry.jsonl` to identify the slot-availability XHR
  4. Set `EVENT_DRIVEN_URL_PATTERN=...` to narrow recording
  5. (future commit) Plug in a JSON parser to skip DOM entirely

The recorder is INTENTIONALLY non-blocking and side-effect-only:
  - Listener never raises out of the checker hot path
  - JSONL flush is lazy (operator-driven cadence, not per-XHR)
  - Skips non-2xx responses (noise)
  - Skips responses that lack expected attributes (defensive)
"""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import date, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_LOG_PATH = Path("xhr_telemetry.jsonl")


class XhrTelemetryRecorder:
    """Per-`_check_date` accumulator of matching XHR responses.

    Construction is cheap. Call `attach(page)` to register the
    Playwright listener; call `detach(page)` then `flush()` to persist
    accumulated records and remove the listener.
    """

    def __init__(
        self,
        url_pattern: str,
        log_path: Path = DEFAULT_LOG_PATH,
        target_date: date | None = None,
    ):
        # If url_pattern is empty, fall back to the target date as the
        # heuristic — most reliable signal we have without operator
        # pre-work.
        self._explicit_pattern = (url_pattern or "").strip()
        self._date_pattern = (
            target_date.isoformat() if target_date is not None else ""
        )
        self.log_path = log_path
        self._buffer: list[dict] = []
        # Prevent two threads from clobbering the buffer; Python is
        # async-on-one-thread but Playwright internals can fire response
        # events from worker threads.
        self._lock = threading.Lock()
        # Used by checker to remove the listener cleanly
        self._on_response_callback = self._on_response

    @property
    def _effective_pattern(self) -> str:
        """The substring that decides whether a URL is "interesting"."""
        return self._explicit_pattern or self._date_pattern

    def _matches(self, url: str) -> bool:
        pattern = self._effective_pattern
        if not pattern:
            return False
        return pattern in url

    def _on_response(self, response) -> None:
        """Playwright response-event listener. Defensive: never raises."""
        try:
            url = getattr(response, "url", "") or ""
            status = getattr(response, "status", 0)
        except Exception:
            return
        if not isinstance(url, str) or not isinstance(status, int):
            return
        if status < 200 or status >= 300:
            return
        if not self._matches(url):
            return
        try:
            req = getattr(response, "request", None)
            method = getattr(req, "method", "") if req is not None else ""
            resource_type = (
                getattr(req, "resource_type", "") if req is not None else ""
            )
        except Exception:
            method = ""
            resource_type = ""

        record = {
            "ts": datetime.now().isoformat(),
            "url": url,
            "status": status,
            "method": method,
            "resource_type": resource_type,
        }
        with self._lock:
            self._buffer.append(record)

    def attach(self, page) -> None:
        """Register the Playwright `response` listener on `page`.
        Safe to call once per recorder; calling twice double-registers."""
        try:
            page.on("response", self._on_response_callback)
        except Exception as e:
            logger.debug(
                f"[xhr-telemetry] page.on('response') raised "
                f"{type(e).__name__}: {e}"
            )

    def detach(self, page) -> None:
        """Remove the listener. Defensive: never raises."""
        try:
            page.remove_listener("response", self._on_response_callback)
        except Exception as e:
            logger.debug(
                f"[xhr-telemetry] page.remove_listener raised "
                f"{type(e).__name__}: {e}"
            )

    async def flush(self) -> int:
        """Append the buffer to log_path as JSONL. Returns the number of
        records written. Empty buffer is a no-op (does not even touch
        the file). Atomic-ish: opens in append mode and writes one line
        at a time, so a crash can lose at most the in-flight line."""
        with self._lock:
            if not self._buffer:
                return 0
            to_write = list(self._buffer)
            self._buffer.clear()

        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                for rec in to_write:
                    f.write(json.dumps(rec) + "\n")
                    f.flush()
                    try:
                        os.fsync(f.fileno())
                    except Exception:
                        pass
            logger.debug(
                f"[xhr-telemetry] flushed {len(to_write)} XHR record(s) "
                f"to {self.log_path}"
            )
            return len(to_write)
        except Exception as e:
            # Don't lose the records on disk failure — restore them
            with self._lock:
                self._buffer = to_write + self._buffer
            logger.warning(
                f"[xhr-telemetry] flush to {self.log_path} failed: "
                f"{type(e).__name__}: {e}; records restored to in-memory "
                "buffer for retry"
            )
            return 0
