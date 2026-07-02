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


# ---------------------------------------------------------------------------
# Tock MUI search-result layout (verified against the 2026-06-12 captured
# failure DOM, booking_failures/faildom_click_20260612_200005_*_2026-06-19.html).
# The bookable control is <button data-testid="booking-card-button">Book</button>,
# the time lives in [data-testid="search-result-time"], and the result cards
# are wrapped by div.Consumer-resultsListVertical inside
# div.SearchModalExperiences-itemTimes — none of which the pre-fix
# class-only selectors matched.
# ---------------------------------------------------------------------------

def test_slot_time_text_includes_search_result_time():
    """slot_time_text must include the real MUI time label so Source 1 of
    the JS time extractor matches (the time is in a sibling subtree, not on
    the button)."""
    sel = get("slot_time_text")
    assert "search-result-time" in sel


def test_slots_container_matches_mui_results_wrapper():
    """slots_container must recognise the MUI results wrapper so
    container-scoped collection doesn't fall back to page-wide on the
    current layout."""
    sel = get("slots_container")
    assert "Consumer-resultsListVertical" in sel
    assert "search-result" in sel
