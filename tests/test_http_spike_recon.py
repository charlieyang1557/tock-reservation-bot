"""Tests for the C.0 recon helper functions (Phase C).

The recon CLI itself is operator-run with headed Playwright. These
tests cover the pure-function pieces:
  - is_interesting_request: filter signal vs noise from XHR firehose
  - format_request_record: serialize a Playwright Request to a
    JSON-safe dict
  - format_response_record: same for Response
"""
import json
from unittest.mock import MagicMock


def test_is_interesting_request_matches_api_paths():
    """Tock's slot-availability/booking XHRs live under known path
    patterns. The filter must catch them."""
    from spikes.http_replay.recon import is_interesting_request

    interesting = [
        "https://www.exploretock.com/api/consumer/availability/restaurant/123",
        "https://api.exploretock.com/v1/availability?date=2026-05-15",
        "https://www.exploretock.com/api/consumer/cart/create",
        "https://www.exploretock.com/api/consumer/checkout/confirm",
        "https://www.exploretock.com/api/consumer/reservation/abc",
        "https://www.exploretock.com/api/consumer/book?id=xyz",
    ]
    for url in interesting:
        assert is_interesting_request(url), f"Should match: {url}"


def test_is_interesting_request_skips_static_assets():
    """Static assets, fonts, and analytics are noise — filter them out."""
    from spikes.http_replay.recon import is_interesting_request

    noise = [
        "https://www.exploretock.com/static/js/main.bundle.js",
        "https://www.exploretock.com/assets/logo.png",
        "https://www.exploretock.com/_next/static/chunks/123.js",
        "https://fonts.googleapis.com/css2?family=Inter",
        "https://www.googletagmanager.com/gtag/js",
        "https://o12345.ingest.sentry.io/api/123/envelope/",
        "https://cdn.segment.io/analytics.min.js",
        "https://www.facebook.com/tr/",
    ]
    for url in noise:
        assert not is_interesting_request(url), f"Should NOT match: {url}"


def test_is_interesting_request_keeps_unknown_xhr_when_unknown():
    """Mostly conservative: when in doubt, keep the request — operator
    can filter further at trace-inspection time."""
    from spikes.http_replay.recon import is_interesting_request

    # An unfamiliar pattern that's still on Tock's domain — keep it.
    assert is_interesting_request(
        "https://www.exploretock.com/some-new-future-endpoint?date=2026-05-15"
    ) is True


def test_format_request_record_strips_auth_headers():
    """Recon traces are committed-adjacent (gitignored output, but
    still on operator's disk). Sensitive headers (Authorization, Cookie,
    CSRF tokens) must NOT be persisted to trace.json."""
    from spikes.http_replay.recon import format_request_record

    fake_request = MagicMock()
    fake_request.url = "https://www.exploretock.com/api/availability"
    fake_request.method = "GET"
    fake_request.headers = {
        "user-agent": "Mozilla/5.0",
        "accept": "application/json",
        "authorization": "Bearer secret-token-here",
        "cookie": "cf_clearance=...; session=...",
        "x-csrf-token": "very-secret-nonce",
        "x-tock-token": "another-secret",
        "referer": "https://www.exploretock.com/restaurant",
    }
    fake_request.resource_type = "xhr"
    fake_request.post_data = None

    rec = format_request_record(fake_request)

    # Safe headers preserved
    assert rec["headers"].get("user-agent") == "Mozilla/5.0"
    assert rec["headers"].get("accept") == "application/json"
    assert rec["headers"].get("referer") == "https://www.exploretock.com/restaurant"

    # Sensitive headers removed/redacted
    assert "authorization" not in rec["headers"]
    assert "cookie" not in rec["headers"]
    # CSRF-style headers removed/marked redacted (keys with secret-looking names)
    assert "x-csrf-token" not in rec["headers"] or rec["headers"]["x-csrf-token"] == "<redacted>"
    assert "x-tock-token" not in rec["headers"] or rec["headers"]["x-tock-token"] == "<redacted>"


def test_format_request_record_redacts_post_body_password_field():
    """If a request body looks like login (contains password), redact it
    before writing to trace."""
    from spikes.http_replay.recon import format_request_record

    fake_request = MagicMock()
    fake_request.url = "https://www.exploretock.com/api/login"
    fake_request.method = "POST"
    fake_request.headers = {}
    fake_request.resource_type = "xhr"
    fake_request.post_data = '{"email":"user@example.com","password":"hunter2"}'

    rec = format_request_record(fake_request)
    body = rec.get("post_data") or ""
    assert "hunter2" not in body, (
        "Password values must be redacted from the trace"
    )
    assert "<redacted>" in body or rec.get("post_data_redacted") is True


def test_format_response_record_includes_status_and_content_type():
    """Response records need status (decision-gate signal) and
    content-type (so operator can spot JSON vs HTML)."""
    from spikes.http_replay.recon import format_response_record

    fake_response = MagicMock()
    fake_response.url = "https://www.exploretock.com/api/availability"
    fake_response.status = 200
    fake_response.headers = {
        "content-type": "application/json; charset=utf-8",
        "content-length": "1234",
    }

    rec = format_response_record(fake_response)
    assert rec["status"] == 200
    assert rec["content_type"] == "application/json; charset=utf-8"


def test_trace_record_is_json_serializable():
    """The full trace record (request + response) must round-trip
    through json.dumps without raising."""
    from spikes.http_replay.recon import format_request_record, format_response_record

    fake_request = MagicMock()
    fake_request.url = "https://www.exploretock.com/api/availability"
    fake_request.method = "GET"
    fake_request.headers = {"accept": "application/json"}
    fake_request.resource_type = "xhr"
    fake_request.post_data = None
    fake_response = MagicMock()
    fake_response.url = fake_request.url
    fake_response.status = 200
    fake_response.headers = {"content-type": "application/json"}

    record = {
        "request": format_request_record(fake_request),
        "response": format_response_record(fake_response),
    }
    # Must not raise
    json.dumps(record)
