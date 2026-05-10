"""Tests for the networkidle removal in warm_session (Phase B1.4).

Tock's restaurant page never reaches networkidle (analytics + Cloudflare
beacons keep firing well past initial render), so the
`page.wait_for_load_state("networkidle", timeout=5000)` call always
times out, burning ~5 s per warm cycle for no benefit. We drop it
entirely — domcontentloaded + the existing _is_logged_in check are
enough to confirm the session.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.mark.asyncio
async def test_warm_session_does_not_block_on_networkidle():
    """warm_session must not call page.wait_for_load_state at all
    (post-B1.4). The 5 s networkidle timeout was pure waste."""
    from src.browser import TockBrowser

    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    browser = TockBrowser(config)

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_load_state = AsyncMock()
    mock_page.wait_for_selector = AsyncMock()
    mock_page.close = AsyncMock()

    browser.new_page = AsyncMock(return_value=mock_page)
    browser._is_logged_in = AsyncMock(return_value=True)
    browser._save_cookies = AsyncMock()

    result = await browser.warm_session()

    assert result is True
    mock_page.wait_for_load_state.assert_not_called(), (
        "warm_session must NOT call wait_for_load_state — the networkidle "
        "wait was the dominant cost and added no signal."
    )


@pytest.mark.asyncio
async def test_warm_session_uses_short_auth_indicator_wait():
    """Codex MEDIUM 3: replace the dropped networkidle wait with a
    bounded wait for the logged_in_indicator selector. If it resolves
    quickly, hydration is done and _is_logged_in is correct. If it times
    out (e.g., logged out), we fall through and let _is_logged_in trigger
    re-login. Total ceiling ~2 s vs the old 5 s — still a net win."""
    from src.browser import TockBrowser

    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    browser = TockBrowser(config)

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(return_value=MagicMock())
    mock_page.close = AsyncMock()

    browser.new_page = AsyncMock(return_value=mock_page)
    browser._is_logged_in = AsyncMock(return_value=True)
    browser._save_cookies = AsyncMock()

    await browser.warm_session()

    # Must wait for the logged_in_indicator with a timeout ≤ 2500 ms
    assert mock_page.wait_for_selector.called, (
        "warm_session must replace networkidle with a bounded wait_for_selector"
    )
    args, kwargs = mock_page.wait_for_selector.call_args
    timeout_ms = kwargs.get("timeout") or (args[1] if len(args) > 1 else None)
    assert timeout_ms is not None and timeout_ms <= 2500, (
        f"Auth-indicator wait must be ≤ 2500 ms; got {timeout_ms}"
    )


@pytest.mark.asyncio
async def test_warm_session_proceeds_when_auth_wait_times_out():
    """If logged_in_indicator never appears (e.g., session expired), the
    bounded wait raises Exception — warm_session must still call
    _is_logged_in to decide whether to re-login."""
    from src.browser import TockBrowser

    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    browser = TockBrowser(config)

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.wait_for_selector = AsyncMock(side_effect=Exception("timeout"))
    mock_page.close = AsyncMock()

    browser.new_page = AsyncMock(return_value=mock_page)
    browser._is_logged_in = AsyncMock(return_value=False)
    browser.login = AsyncMock(return_value=True)
    browser._save_cookies = AsyncMock()

    result = await browser.warm_session()

    # When auth indicator times out, fall through to _is_logged_in → re-login.
    browser._is_logged_in.assert_awaited()
    assert result is True


@pytest.mark.asyncio
async def test_warm_session_still_uses_domcontentloaded_in_goto():
    """Sanity: the goto call still uses domcontentloaded for the wait_until
    so we don't regress to networkidle there either."""
    from src.browser import TockBrowser

    config = MagicMock()
    config.restaurant_slug = "test"
    config.headless = True
    browser = TockBrowser(config)

    mock_page = AsyncMock()
    mock_page.goto = AsyncMock()
    mock_page.close = AsyncMock()
    browser.new_page = AsyncMock(return_value=mock_page)
    browser._is_logged_in = AsyncMock(return_value=True)
    browser._save_cookies = AsyncMock()

    await browser.warm_session()

    mock_page.goto.assert_called_once()
    args, kwargs = mock_page.goto.call_args
    assert kwargs.get("wait_until") == "domcontentloaded"
