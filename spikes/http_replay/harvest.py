"""Phase C.1 — Harvest auth cookies from a logged-in Playwright session.

Reads the bot's `session_cookies.json` (written by `TockBrowser._save_cookies()`),
keeps the cookies that authenticate against Tock (cf_clearance, session
token, etc.), drops analytics noise, and writes a flat
{name: value} JSON file ready to be consumed by aiohttp.ClientSession.

Operator workflow:
  python -m spikes.http_replay.harvest \
      --input session_cookies.json \
      --output spikes/http_replay/aiohttp_cookies.json

Then feed the output to `probe.py` (Phase C.2).

Sensitive output: the resulting file holds live auth tokens.
GITIGNORED — never commit.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_INPUT = Path("session_cookies.json")
DEFAULT_OUTPUT = Path("spikes/http_replay/aiohttp_cookies.json")

# Cookie name patterns that are PURELY analytics/tracking — drop them.
# Anything not in this set is conservatively kept (auth might use an
# unfamiliar name).
_TRACKING_NAME_PREFIXES = (
    "_ga",
    "_gid",
    "_gat",
    "_fbp",
    "_fbc",
    "ajs_",      # Segment
    "intercom-",
    "_hj",       # Hotjar
    "_uet",      # Microsoft UET
    "mp_",       # Mixpanel
    "_pin_",     # Pinterest
)


def _is_tracking_cookie(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(lower.startswith(prefix) for prefix in _TRACKING_NAME_PREFIXES)


def _is_tock_domain(domain: str) -> bool:
    if not domain:
        return False
    lower = domain.lower().lstrip(".")
    return lower.endswith("exploretock.com") or lower.endswith("tock.com")


def filter_auth_cookies(cookies: list[dict]) -> list[dict]:
    """Keep only cookies that could plausibly authenticate Tock requests.

    Drops:
      - cookies whose name matches a known tracker prefix
      - cookies whose domain isn't on tock.com / exploretock.com

    Conservatively KEEPS unknown cookie names — better to send extra
    than to lock the operator out by stripping a future auth cookie.
    """
    out = []
    for c in cookies:
        name = c.get("name", "")
        domain = c.get("domain", "")
        if _is_tracking_cookie(name):
            continue
        if not _is_tock_domain(domain):
            continue
        out.append(c)
    return out


def to_aiohttp_format(cookies: list[dict]) -> dict[str, str]:
    """Reduce Playwright cookies to the {name: value} dict that
    `aiohttp.ClientSession(cookies=...)` accepts directly."""
    return {c["name"]: c["value"] for c in cookies if "name" in c and "value" in c}


def harvest_from_file(input_path: Path, output_path: Path) -> int:
    """Read Playwright cookies, filter, write aiohttp-ready dict.

    Returns the number of cookies written.

    Raises:
        FileNotFoundError: input doesn't exist (operator must run the
            bot once to create session_cookies.json)
        ValueError: input is not valid JSON or not a list
    """
    if not input_path.exists():
        raise FileNotFoundError(
            f"{input_path} not found — run the bot at least once to "
            "create session_cookies.json (e.g. `python main.py --once`)"
        )
    raw_text = input_path.read_text()
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise ValueError(
            f"{input_path} is not valid JSON: {e}. Delete it and re-run "
            "the bot to recreate."
        ) from e
    if not isinstance(data, list):
        raise ValueError(
            f"{input_path} top-level must be a list of cookie objects; "
            f"got {type(data).__name__}"
        )
    auth_cookies = filter_auth_cookies(data)
    aiohttp_dict = to_aiohttp_format(auth_cookies)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(aiohttp_dict, indent=2))
    return len(aiohttp_dict)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Phase C.1 — extract auth cookies from session_cookies.json "
            "and rewrite them for aiohttp.ClientSession consumption."
        )
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    args = _parse_args(argv)
    try:
        n = harvest_from_file(args.input, args.output)
    except (FileNotFoundError, ValueError) as e:
        print(f"[harvest] {e}", file=sys.stderr)
        return 2
    print(f"[harvest] Wrote {n} auth cookie(s) to {args.output}")
    print("[harvest] WARNING: this file holds live auth tokens. Do NOT commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
