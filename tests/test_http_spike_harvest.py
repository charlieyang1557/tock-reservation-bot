"""Tests for the C.1 cookie harvest helpers (Phase C).

The harvest CLI extracts auth-relevant cookies from `session_cookies.json`
(written by the Playwright bot) and rewrites them in a shape `aiohttp`
or `httpx` can consume. Pure-function tests cover the conversion logic.
"""
from pathlib import Path
import json


def test_filter_auth_cookies_keeps_cf_clearance_and_session():
    """The cookies that matter for impersonating a logged-in browser:
    cf_clearance (Cloudflare anti-bot pass), session/auth tokens, and
    the bot's own JWT-shaped tokens. Filter out tracking/analytics
    cookies (they don't authenticate)."""
    from spikes.http_replay.harvest import filter_auth_cookies

    raw = [
        {"name": "cf_clearance", "value": "abc", "domain": ".exploretock.com", "path": "/"},
        {"name": "_ga", "value": "GA1.2.123", "domain": ".exploretock.com", "path": "/"},
        {"name": "session", "value": "deadbeef", "domain": ".exploretock.com", "path": "/"},
        {"name": "_gid", "value": "GA1.2.456", "domain": ".exploretock.com", "path": "/"},
        {"name": "tock_session_id", "value": "xyz", "domain": ".exploretock.com", "path": "/"},
        {"name": "ajs_user_id", "value": "abc", "domain": ".exploretock.com", "path": "/"},
        {"name": "_fbp", "value": "fb.1.123", "domain": ".exploretock.com", "path": "/"},
    ]
    out = filter_auth_cookies(raw)
    names = {c["name"] for c in out}

    # Must keep these
    assert "cf_clearance" in names
    assert "session" in names
    assert "tock_session_id" in names

    # Must drop these (analytics)
    assert "_ga" not in names
    assert "_gid" not in names
    assert "ajs_user_id" not in names
    assert "_fbp" not in names


def test_filter_auth_cookies_keeps_unknown_tock_cookies_conservatively():
    """When a cookie name doesn't match any known tracker AND lives on
    exploretock.com, KEEP it — better safe than locked out."""
    from spikes.http_replay.harvest import filter_auth_cookies

    raw = [
        {"name": "future_unknown", "value": "x", "domain": ".exploretock.com", "path": "/"},
    ]
    out = filter_auth_cookies(raw)
    assert any(c["name"] == "future_unknown" for c in out)


def test_filter_auth_cookies_drops_non_tock_cookies():
    """A cookie scoped to a different domain (e.g. third-party widget)
    cannot impersonate the user on Tock — drop it."""
    from spikes.http_replay.harvest import filter_auth_cookies

    raw = [
        {"name": "session", "value": "x", "domain": ".other-site.com", "path": "/"},
        {"name": "session", "value": "y", "domain": ".exploretock.com", "path": "/"},
    ]
    out = filter_auth_cookies(raw)
    domains = {c["domain"] for c in out}
    assert ".exploretock.com" in domains
    assert ".other-site.com" not in domains


def test_to_aiohttp_format_strips_playwright_only_fields():
    """Playwright cookies have fields aiohttp's CookieJar doesn't
    consume (sameSite, expires float vs int, etc.). Output must be
    safe to pass to `aiohttp.ClientSession(cookies=...)`."""
    from spikes.http_replay.harvest import to_aiohttp_format

    raw = [{
        "name": "cf_clearance",
        "value": "abc",
        "domain": ".exploretock.com",
        "path": "/",
        "expires": 1234567890.0,
        "httpOnly": True,
        "secure": True,
        "sameSite": "Lax",
    }]
    out = to_aiohttp_format(raw)
    # aiohttp wants {name: value} for the simple constructor case
    assert out == {"cf_clearance": "abc"}


def test_harvest_from_file_reads_session_cookies(tmp_path):
    """End-to-end: load session_cookies.json shape, filter, write the
    aiohttp-ready file."""
    from spikes.http_replay.harvest import harvest_from_file

    src = tmp_path / "session_cookies.json"
    src.write_text(json.dumps([
        {"name": "cf_clearance", "value": "abc",
         "domain": ".exploretock.com", "path": "/"},
        {"name": "_ga", "value": "GA1", "domain": ".exploretock.com", "path": "/"},
    ]))
    dst = tmp_path / "aiohttp_cookies.json"

    n = harvest_from_file(src, dst)
    assert n == 1  # cf_clearance kept; _ga dropped

    written = json.loads(dst.read_text())
    assert "cf_clearance" in written
    assert "_ga" not in written


def test_harvest_handles_missing_input_file(tmp_path):
    """If session_cookies.json doesn't exist, surface a clear error
    rather than silently writing an empty file."""
    from spikes.http_replay.harvest import harvest_from_file

    src = tmp_path / "missing.json"
    dst = tmp_path / "out.json"

    import pytest
    with pytest.raises(FileNotFoundError):
        harvest_from_file(src, dst)


def test_harvest_handles_corrupt_input_file(tmp_path):
    """If session_cookies.json is malformed JSON, surface ValueError
    so the operator knows to delete + re-login."""
    from spikes.http_replay.harvest import harvest_from_file

    src = tmp_path / "corrupt.json"
    src.write_text("{not valid json")
    dst = tmp_path / "out.json"

    import pytest
    with pytest.raises(ValueError):
        harvest_from_file(src, dst)
