"""Tests for ``qcu.common.normalize``."""

from __future__ import annotations

from qcu.common.normalize import (
    NORMALIZED_ROLES,
    clean_text,
    from_aria,
    from_ax,
    from_uia,
    normalize,
    wait_ms,
)


# ---------------------------------------------------------------------------
# ARIA
# ---------------------------------------------------------------------------


def test_aria_button():
    assert from_aria("button") == "button"


def test_aria_textbox_to_edit():
    assert from_aria("textbox") == "edit"


def test_aria_unknown_to_group_when_empty():
    assert from_aria(None) == "group"
    assert from_aria("") == "group"


def test_aria_unknown_string_to_other():
    assert from_aria("SomeMadeUpRole") == "other"


def test_aria_static_text():
    assert from_aria("StaticText") == "text"


def test_aria_case_insensitive_fallback():
    # Lower-case variant not in the table; should fall through to "other"
    # unless it hits the substring heuristic.
    assert normalize("BUTTON") == "button"  # aria table is mixed-case but contains this
    # Generic unknown should land in "other"
    assert normalize("NotARole", source="aria") == "other"


# ---------------------------------------------------------------------------
# AX
# ---------------------------------------------------------------------------


def test_ax_button():
    assert from_ax("AXButton") == "button"


def test_ax_textfield_to_edit():
    assert from_ax("AXTextField") == "edit"


def test_ax_unknown_to_group_when_empty():
    assert from_ax(None) == "group"


def test_ax_unknown_to_other():
    assert from_ax("AXSomethingWeird") == "other"


# ---------------------------------------------------------------------------
# UIA
# ---------------------------------------------------------------------------


def test_uia_button():
    assert from_uia("ButtonControl") == "button"


def test_uia_edit():
    assert from_uia("EditControl") == "edit"


def test_uia_strips_controltypeid():
    # Some versions of uiautomation return "ButtonControlTypeId".
    assert from_uia("ButtonControlTypeId") == "button"


def test_uia_unknown_to_other():
    assert from_uia("MysteryControl") == "other"


# ---------------------------------------------------------------------------
# normalize() entry point
# ---------------------------------------------------------------------------


def test_normalize_auto_dispatches_by_role():
    assert normalize("button", source="auto") == "button"
    assert normalize("AXButton", source="auto") == "button"
    assert normalize("ButtonControl", source="auto") == "button"


def test_normalize_role_set_includes_common():
    for r in ("button", "link", "edit", "checkbox", "tab", "menu_item"):
        assert r in NORMALIZED_ROLES


# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------


def test_clean_text_collapses_whitespace():
    assert clean_text("a  b\n\nc\td") == "a b c d"


def test_clean_text_truncates():
    s = "x" * 500
    out = clean_text(s, max_len=10)
    assert len(out) <= 10
    assert out.endswith("…")


def test_clean_text_empty():
    assert clean_text("") == ""
    assert clean_text(None) == ""


# ===========================================================================
# wait_ms — the "2 seconds actually waited 100ms" bug fix
# ===========================================================================


def test_wait_ms_default_is_100():
    """No unit given → 100 ms (historical default preserved)."""
    assert wait_ms({}) == 100
    assert wait_ms(None) == 100


def test_wait_ms_seconds_key():
    """The bug: {seconds: 2} used to fall through to 100ms. Now → 2000."""
    assert wait_ms({"seconds": 2}) == 2000
    assert wait_ms({"seconds": 0.5}) == 500


def test_wait_ms_s_key():
    """Short alias {s} also resolves."""
    assert wait_ms({"s": 2}) == 2000


def test_wait_ms_ms_key_takes_precedence():
    """When both ms and seconds are present, ms wins (explicit > implicit)."""
    assert wait_ms({"ms": 2000, "seconds": 5}) == 2000


def test_wait_ms_garbage_falls_back_to_default():
    assert wait_ms({"ms": "abc"}) == 100
    assert wait_ms({"seconds": "xyz"}) == 100


def test_wait_ms_never_negative():
    assert wait_ms({"seconds": -5}) == 0
    assert wait_ms({"ms": -10}) == 0