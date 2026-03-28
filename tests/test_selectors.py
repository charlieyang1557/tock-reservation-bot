"""Tests for selector keys used by the slot detection flow."""

from src.selectors import get


def test_all_day_button_selector_exists():
    """all_day_button selector should match any in-month calendar day."""
    sel = get("all_day_button")
    assert "ConsumerCalendar-day" in sel
    assert "is-available" not in sel  # must NOT require is-available


def test_book_now_button_selector_exists():
    """book_now_button selector should exist."""
    sel = get("book_now_button")
    assert "Book now" in sel or "book" in sel.lower()
