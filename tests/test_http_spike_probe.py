"""Tests for the C.2 probe classifier (Phase C).

The probe CLI fires a real HTTP GET; that's an integration test the
operator runs by hand. The pure-function piece tested here is
`classify_response`: given (status, headers, body), decide whether
the response is PASS / BLOCKED / UNCLEAR.

Decision gate logic:
  PASS    — 2xx + JSON content-type + non-empty body. C.3 can proceed.
  BLOCKED — 403 / 503, OR HTML containing CF challenge markers.
            Spike fails — abort, no src/ changes.
  UNCLEAR — anything else (200 + HTML, 200 + empty body, 4xx other
            than 403, 5xx other than 503). Operator inspects manually.
"""


def test_classify_response_pass_on_200_json_with_body():
    """Happy path: 200 OK + application/json + non-empty body = PASS."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=200,
        headers={"content-type": "application/json; charset=utf-8"},
        body=b'{"slots":[{"time":"5:00 PM"}]}',
    )
    assert verdict.status == "PASS"
    assert "json" in verdict.reason.lower()


def test_classify_response_blocked_on_403():
    """403 Forbidden = BLOCKED. Tock or CF rejected the request."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=403,
        headers={"content-type": "text/html"},
        body=b"<html>Access denied</html>",
    )
    assert verdict.status == "BLOCKED"
    assert "403" in verdict.reason


def test_classify_response_blocked_on_503():
    """503 Service Unavailable (often CF rate-limit) = BLOCKED."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=503,
        headers={"content-type": "text/html"},
        body=b"<html>Sorry...</html>",
    )
    assert verdict.status == "BLOCKED"


def test_classify_response_blocked_on_cf_challenge_html():
    """200 OK + HTML body containing a CF challenge marker = BLOCKED.
    CF challenges use 200 status with an interstitial HTML page."""
    from spikes.http_replay.probe import classify_response

    cf_body = (
        b"<html><head><title>Just a moment...</title></head>"
        b'<body><div class="cf-turnstile"></div></body></html>'
    )
    verdict = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body=cf_body,
    )
    assert verdict.status == "BLOCKED"
    assert "cloudflare" in verdict.reason.lower() or "cf" in verdict.reason.lower()


def test_classify_response_blocked_on_cf_text_signal():
    """The CF interstitial text 'Verify you are human' is also a CF marker."""
    from spikes.http_replay.probe import classify_response

    cf_body = (
        b"<html><body><h1>Verify you are human by completing the action below.</h1>"
        b"</body></html>"
    )
    verdict = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body=cf_body,
    )
    assert verdict.status == "BLOCKED"


def test_classify_response_unclear_on_200_html_no_cf_marker():
    """200 OK + HTML without CF markers = UNCLEAR. Could be a redirect
    page, an empty SPA shell, or a Tock product page. Operator should
    inspect manually."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=200,
        headers={"content-type": "text/html"},
        body=b"<html><body>Welcome to the restaurant page</body></html>",
    )
    assert verdict.status == "UNCLEAR"


def test_classify_response_unclear_on_200_json_empty_body():
    """200 OK + JSON content-type but EMPTY body = UNCLEAR. The endpoint
    might exist but return nothing for this query, which doesn't tell
    us the spike is feasible."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=200,
        headers={"content-type": "application/json"},
        body=b"",
    )
    assert verdict.status == "UNCLEAR"


def test_classify_response_unclear_on_404():
    """404 might just mean wrong URL — UNCLEAR, not BLOCKED."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=404,
        headers={"content-type": "text/html"},
        body=b"Not Found",
    )
    assert verdict.status == "UNCLEAR"


def test_classify_response_unclear_on_500():
    """500 = server bug, not necessarily a block. UNCLEAR."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=500,
        headers={"content-type": "text/html"},
        body=b"Internal Server Error",
    )
    assert verdict.status == "UNCLEAR"


def test_classify_response_pass_on_204_no_content():
    """204 No Content is a valid success for some POST endpoints. PASS
    (the API is reachable AND not blocking)."""
    from spikes.http_replay.probe import classify_response

    verdict = classify_response(
        status=204,
        headers={},
        body=b"",
    )
    assert verdict.status == "PASS"
