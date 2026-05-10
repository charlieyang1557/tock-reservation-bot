"""Tests for src/selector_metrics.py (Phase B2.4).

Records which DOM selectors successfully matched in production so the team
can re-order fallbacks by data instead of guessing.

Design notes pinned by these tests:
- Lazy in-memory aggregation (no per-call disk I/O)
- Periodic flush merges with on-disk JSON (read-modify-write)
- Default-empty when file is missing or corrupt (warn, don't crash)
- Thread-safe so the API can be called from anywhere
"""
import json
import threading
from pathlib import Path

import pytest

from src import selector_metrics


@pytest.fixture(autouse=True)
def _reset_in_memory_state():
    """Reset the module-level counter between tests."""
    selector_metrics._reset_for_tests()
    yield
    selector_metrics._reset_for_tests()


def test_record_match_increments_counter():
    """Two record_match calls with same key+selector -> count is 2."""
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("slot_button", "button.foo")
    snapshot = selector_metrics._snapshot_for_tests()
    assert snapshot["slot_button"]["button.foo"] == 2


def test_record_match_separates_keys_and_selectors():
    """Counts are bucketed by (key, selector)."""
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("slot_button", "button.bar")
    selector_metrics.record_match("calendar_day", "button.foo")
    snapshot = selector_metrics._snapshot_for_tests()
    assert snapshot["slot_button"]["button.foo"] == 1
    assert snapshot["slot_button"]["button.bar"] == 1
    assert snapshot["calendar_day"]["button.foo"] == 1


def test_record_match_ignores_blank_inputs():
    """Empty key or selector is silently dropped (defensive against bugs)."""
    selector_metrics.record_match("", "button.foo")
    selector_metrics.record_match("slot_button", "")
    selector_metrics.record_match(None, None)  # type: ignore[arg-type]
    snapshot = selector_metrics._snapshot_for_tests()
    assert snapshot == {}


def test_flush_writes_in_memory_to_file(tmp_path: Path):
    """flush() persists in-memory counts to JSON file."""
    path = tmp_path / "metrics.json"
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("calendar_day", "div.bar")

    selector_metrics.flush(path)

    on_disk = json.loads(path.read_text())
    assert on_disk == {
        "slot_button": {"button.foo": 2},
        "calendar_day": {"div.bar": 1},
    }


def test_flush_merges_with_existing_file(tmp_path: Path):
    """Pre-existing file counts are summed with the new in-memory counts."""
    path = tmp_path / "metrics.json"
    pre_existing = {
        "slot_button": {"button.foo": 5, "button.bar": 3},
        "calendar_day": {"div.day": 7},
    }
    path.write_text(json.dumps(pre_existing))

    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("checkout", "form.checkout")

    selector_metrics.flush(path)

    on_disk = json.loads(path.read_text())
    assert on_disk == {
        "slot_button": {"button.foo": 7, "button.bar": 3},
        "calendar_day": {"div.day": 7},
        "checkout": {"form.checkout": 1},
    }


def test_flush_resets_in_memory_after_flush(tmp_path: Path):
    """In-memory state is empty after a successful flush."""
    path = tmp_path / "metrics.json"
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.flush(path)

    snapshot = selector_metrics._snapshot_for_tests()
    assert snapshot == {}

    # And a second flush is a no-op (no double-counting)
    selector_metrics.flush(path)
    on_disk = json.loads(path.read_text())
    assert on_disk == {"slot_button": {"button.foo": 1}}


def test_flush_with_empty_in_memory_does_not_create_file(tmp_path: Path):
    """No counts queued -> nothing to flush, file is not created."""
    path = tmp_path / "metrics.json"
    selector_metrics.flush(path)
    assert not path.exists()


def test_flush_with_empty_in_memory_preserves_existing_file(tmp_path: Path):
    """No counts queued + existing file -> file is left alone."""
    path = tmp_path / "metrics.json"
    pre = {"slot_button": {"button.foo": 5}}
    path.write_text(json.dumps(pre))
    selector_metrics.flush(path)
    assert json.loads(path.read_text()) == pre


def test_read_metrics_returns_data(tmp_path: Path):
    """read_metrics() returns the dict from disk."""
    path = tmp_path / "metrics.json"
    data = {"slot_button": {"button.foo": 5}}
    path.write_text(json.dumps(data))
    assert selector_metrics.read_metrics(path) == data


def test_read_metrics_returns_empty_for_missing_file(tmp_path: Path):
    """read_metrics() on a missing file returns {}, no exception."""
    path = tmp_path / "does_not_exist.json"
    assert selector_metrics.read_metrics(path) == {}


def test_read_metrics_returns_empty_for_corrupt_file(tmp_path: Path, caplog):
    """read_metrics() on a corrupt file returns {} and logs a warning."""
    path = tmp_path / "metrics.json"
    path.write_text("{not valid json")

    import logging

    with caplog.at_level(logging.WARNING):
        result = selector_metrics.read_metrics(path)

    assert result == {}
    assert any("corrupt" in r.message.lower() or "invalid" in r.message.lower()
               for r in caplog.records)


def test_read_metrics_returns_empty_for_non_dict_root(tmp_path: Path):
    """read_metrics() on a JSON list (wrong shape) returns {}."""
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps(["not", "a", "dict"]))
    assert selector_metrics.read_metrics(path) == {}


def test_flush_handles_corrupt_existing_file(tmp_path: Path):
    """flush() on top of a corrupt file should not lose new data."""
    path = tmp_path / "metrics.json"
    path.write_text("garbage{")

    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.flush(path)

    # Corrupt content overwritten with our new counts
    on_disk = json.loads(path.read_text())
    assert on_disk == {"slot_button": {"button.foo": 1}}


def test_concurrent_record_match_is_thread_safe():
    """N threads each calling record_match -> total count = N (no lost updates)."""
    n_threads = 50
    iters_per_thread = 20
    target_total = n_threads * iters_per_thread

    def worker():
        for _ in range(iters_per_thread):
            selector_metrics.record_match("slot_button", "button.foo")

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    snapshot = selector_metrics._snapshot_for_tests()
    assert snapshot["slot_button"]["button.foo"] == target_total


def test_flush_merges_atomically_under_concurrent_records(tmp_path: Path):
    """Recording while a flush is in progress should not lose counts.

    We can't easily inject a delay into flush(), so this test stages
    counts, flushes, immediately records more, flushes again, and asserts
    the final on-disk total equals the sum of all record_match calls.
    """
    path = tmp_path / "metrics.json"
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.flush(path)

    selector_metrics.record_match("slot_button", "button.foo")
    selector_metrics.flush(path)

    on_disk = json.loads(path.read_text())
    assert on_disk == {"slot_button": {"button.foo": 3}}


def test_flush_with_long_selector_string(tmp_path: Path):
    """Selectors can be long multi-line CSS comma lists. JSON handles it natively."""
    path = tmp_path / "metrics.json"
    long_sel = (
        'button:text("Complete purchase"), '
        'button:text("Complete reservation"), '
        'button:text("Confirm reservation"), '
        'button:text("Reserve now"), '
        'button[type="submit"]:visible'
    )
    selector_metrics.record_match("confirm_button", long_sel)
    selector_metrics.flush(path)

    on_disk = json.loads(path.read_text())
    assert on_disk["confirm_button"][long_sel] == 1


def test_flush_default_path_argument():
    """Calling flush() with no path should not crash; default = selector_metrics.json."""
    # We don't want to write into the real cwd. Just assert the call shape works
    # against an empty in-memory state (no file write happens).
    selector_metrics.flush()  # noqa: no args, uses default path
    # No assertion needed beyond "did not raise"


def test_read_metrics_default_path_argument():
    """Calling read_metrics() with no path should not crash."""
    # If selector_metrics.json doesn't exist in cwd, expect {}
    # If it does, accept anything dict-shaped.
    result = selector_metrics.read_metrics()
    assert isinstance(result, dict)
