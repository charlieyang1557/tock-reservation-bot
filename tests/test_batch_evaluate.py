"""Tests for batched page.evaluate() helpers."""
import pytest
from src.selectors import get_slot_button_selectors


class TestBatchSlotDetect:
    def test_selectors_list_valid(self):
        selectors = get_slot_button_selectors()
        assert len(selectors) >= 4
        assert all(isinstance(s, str) for s in selectors)

    def test_css_vs_playwright_split(self):
        """At least some selectors are CSS-compatible (no :has-text etc)."""
        selectors = get_slot_button_selectors()
        css = [s for s in selectors if not any(pw in s for pw in [':has-text', ':text(', ':visible'])]
        assert len(css) >= 2, "Need at least 2 CSS-compatible selectors for fast evaluate path"
