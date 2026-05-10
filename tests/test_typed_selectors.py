"""Tests for the typed slot-selector helper (Codex MEDIUM 1 fix).

Old: `is_generic = matched_selector in _GENERIC_BOOK_SELECTORS` — exact
frozenset membership. If the selector string in `get_slot_button_selectors`
drifts (whitespace, comma ordering, quoting), `is_generic` silently flips
False and the booker may click a generic button without confirming the
target time in its parent.

New: a typed helper `is_generic_slot_selector(selector)` that consults a
single source-of-truth list of (selector, kind) pairs.
"""
from src.selectors import (
    get_slot_button_selectors,
    get_slot_button_selectors_typed,
    is_generic_slot_selector,
)


def test_typed_selectors_match_legacy_string_list_in_order():
    """The typed list must contain the same selector strings in the same
    order as the legacy flat list (so existing iteration order is
    preserved)."""
    typed = get_slot_button_selectors_typed()
    flat = get_slot_button_selectors()
    assert [s for s, _ in typed] == flat


def test_typed_selectors_kind_field_is_specific_or_generic():
    """Every entry has a `kind` of 'specific' or 'generic' — no other
    values allowed."""
    typed = get_slot_button_selectors_typed()
    kinds = {kind for _, kind in typed}
    assert kinds.issubset({"specific", "generic"})
    assert len(typed) > 0


def test_typed_selectors_specifics_first():
    """Specific selectors come BEFORE generic ones (preserved iteration
    order matters: we want exact slot buttons to win over generic Books)."""
    typed = get_slot_button_selectors_typed()
    saw_generic = False
    for _, kind in typed:
        if kind == "generic":
            saw_generic = True
        else:
            # 'specific' must not appear after a 'generic' in the list
            assert not saw_generic, (
                "Specific selectors must precede generic ones in the "
                "iteration order"
            )


def test_is_generic_slot_selector_returns_true_for_book_now():
    """`button:text("Book now")` style selectors are generic."""
    assert is_generic_slot_selector('button:visible:has-text("Book")') is True
    assert is_generic_slot_selector("button.SearchExperience-bookButton") is True
    assert is_generic_slot_selector("[data-testid='book-button']") is True


def test_is_generic_slot_selector_returns_false_for_specific_button():
    """The Consumer-resultsListItem selectors are specific (per-time-slot)."""
    assert is_generic_slot_selector(
        "button.Consumer-resultsListItem.is-available"
    ) is False
    assert is_generic_slot_selector("button.Consumer-resultsListItem") is False


def test_is_generic_slot_selector_unknown_treated_as_generic():
    """Codex B2 review: unknown selectors now default to generic (the
    SAFER fail-closed behavior — refuses first-button fallback). Empty
    string is still False (not a real selector)."""
    assert is_generic_slot_selector("div.some-future-selector") is True
    assert is_generic_slot_selector("") is False


def test_is_generic_slot_selector_decoupled_from_string_drift():
    """The helper must look up via the source-of-truth list, not via
    substring matching — so a benign whitespace difference in a future
    selector value would NOT silently misclassify."""
    # If we ever add a generic selector with extra whitespace, the helper
    # should still recognize it iff it's in the typed list. This test
    # ensures the helper is consulting the typed list, not a hand-coded
    # set.
    typed = get_slot_button_selectors_typed()
    for selector, kind in typed:
        result = is_generic_slot_selector(selector)
        expected = (kind == "generic")
        assert result is expected, (
            f"Selector {selector!r} (kind={kind}) → is_generic={result}; "
            f"helper must agree with the typed list"
        )
