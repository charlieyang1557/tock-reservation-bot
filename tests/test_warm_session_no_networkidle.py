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
