"""Shared pytest fixtures and helpers for Tock bot tests."""
from unittest.mock import AsyncMock, MagicMock


def _zero_count_locator() -> MagicMock:
    """Return a Locator-like mock whose count() resolves to 0.

    Used to stub the slots_container locator in checker tests so the
    fallback (page-wide) path is exercised — not the scoped path.
    """
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    return loc


def make_page_locator(real_button_locator: MagicMock) -> MagicMock:
    """Build a page.locator side_effect that:
    - Returns a count=0 stub for the slots_container selector
    - Returns the provided real button locator for everything else

    Lets tests construct mock pages without breaking the new container-scope
    code path in AvailabilityChecker._collect_slots_multi.

    Uses exact equality against SELECTORS["slots_container"] so that button
    selectors that share substrings with the container selector are not
    accidentally stubbed out.
    """
    zero = _zero_count_locator()

    def side_effect(selector: str) -> MagicMock:
        from src.selectors import SELECTORS
        container_sel = SELECTORS.get("slots_container", "")
        if selector == container_sel:
            return zero
        return real_button_locator

    return MagicMock(side_effect=side_effect)
