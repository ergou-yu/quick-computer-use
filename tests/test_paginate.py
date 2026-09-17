"""Unit tests for the desktop pagination helper."""

from __future__ import annotations

import pytest

from qcu.common.types import Element, Rect
from qcu.layers._paginate import apply_pagination_and_compact


def _e(ref: str, role: str = "button", name: str | None = None, value: str | None = None):
    return Element(ref=ref, role=role, name=name, value=value)


# ===========================================================================
# Baseline: no pagination, no filter — full pass-through
# ===========================================================================


def test_no_options_returns_all():
    elems = [_e("ref_0"), _e("ref_1"), _e("ref_2")]
    sliced, meta = apply_pagination_and_compact(elems)
    assert len(sliced) == 3
    assert meta["n_interactive"] == 3
    assert meta["n_total"] == 3
    assert meta["n_returned"] == 3
    assert meta["tree_truncated"] is False


# ===========================================================================
# limit + offset
# ===========================================================================


def test_limit_only():
    elems = [_e(f"ref_{i}") for i in range(10)]
    sliced, meta = apply_pagination_and_compact(elems, limit=3)
    assert len(sliced) == 3
    assert sliced[0].ref == "ref_0"
    assert sliced[2].ref == "ref_2"
    assert meta["n_returned"] == 3
    assert meta["n_total"] == 10
    assert meta["tree_truncated"] is True


def test_offset_only_truncates_only_when_actually_skipping():
    elems = [_e("ref_0"), _e("ref_1"), _e("ref_2")]
    # offset = 0 alone is no-op.
    sliced, meta = apply_pagination_and_compact(elems, offset=0)
    assert meta["tree_truncated"] is False
    # offset > 0 marks truncated even when limit is unset.
    sliced, meta = apply_pagination_and_compact(elems, offset=1)
    assert len(sliced) == 2
    assert sliced[0].ref == "ref_1"
    assert meta["tree_truncated"] is True


def test_offset_plus_limit():
    elems = [_e(f"ref_{i}") for i in range(10)]
    sliced, meta = apply_pagination_and_compact(elems, offset=5, limit=3)
    assert len(sliced) == 3
    assert sliced[0].ref == "ref_5"
    assert sliced[2].ref == "ref_7"
    assert meta["n_returned"] == 3
    assert meta["n_total"] == 10
    assert meta["tree_truncated"] is True


def test_offset_beyond_end_returns_empty():
    elems = [_e("ref_0"), _e("ref_1")]
    sliced, meta = apply_pagination_and_compact(elems, offset=99)
    assert len(sliced) == 0
    assert meta["tree_truncated"] is True


# ===========================================================================
# role filter
# ===========================================================================


def test_role_filter_drops_non_matching():
    elems = [
        _e("r0", role="button"),
        _e("r1", role="link"),
        _e("r2", role="button"),
        _e("r3", role="edit"),
    ]
    sliced, meta = apply_pagination_and_compact(elems, role_filter="button")
    assert [e.ref for e in sliced] == ["r0", "r2"]
    assert meta["n_interactive"] == 4
    assert meta["n_total"] == 2  # after filter


# ===========================================================================
# name substring
# ===========================================================================


def test_name_query_case_insensitive_on_name():
    elems = [
        _e("r0", name="Save As"),
        _e("r1", name="Open"),
        _e("r2", name="Save"),
        _e("r3", name=None),
    ]
    sliced, meta = apply_pagination_and_compact(elems, name_query="save")
    assert {e.ref for e in sliced} == {"r0", "r2"}


def test_name_query_also_matches_value():
    elems = [
        _e("r0", name="input", value="alice@example.com"),
        _e("r1", name="input", value="bob"),
    ]
    sliced, meta = apply_pagination_and_compact(elems, name_query="alice")
    assert [e.ref for e in sliced] == ["r0"]


# ===========================================================================
# compact mode drops bulky fields
# ===========================================================================


def test_compact_drops_properties_and_keeps_required():
    rich = Element(
        ref="ref_0",
        role="button",
        name="Save",
        value=None,
        bounds=Rect(1, 2, 3, 4),
        enabled=False,
        focused=True,
        properties={"description": "save", "raw_role": "AXButton", "depth": 3},
    )
    sliced, _ = apply_pagination_and_compact([rich], compact=True)
    assert len(sliced) == 1
    e = sliced[0]
    # Required action-planning fields survive.
    assert e.ref == "ref_0"
    assert e.role == "button"
    assert e.name == "Save"
    assert e.bounds is not None
    assert e.enabled is False
    assert e.focused is True
    # Bulky fields are gone.
    assert e.properties == {} or e.properties is None


def test_compact_default_keeps_properties_intact():
    rich = Element(
        ref="ref_0",
        role="button",
        name="Save",
        properties={"description": "save"},
    )
    sliced, _ = apply_pagination_and_compact([rich], compact=False)
    assert sliced[0].properties == {"description": "save"}


# ===========================================================================
# interaction between filter + pagination
# ===========================================================================


def test_filter_then_limit():
    elems = [
        _e("r0", role="button", name="Apple 1"),
        _e("r1", role="link", name="Apple 2"),
        _e("r2", role="button", name="Apple 3"),
        _e("r3", role="button", name="Apple 4"),
    ]
    sliced, meta = apply_pagination_and_compact(
        elems, role_filter="button", limit=2
    )
    assert [e.ref for e in sliced] == ["r0", "r2"]
    assert meta["n_interactive"] == 4
    assert meta["n_total"] == 3  # after role filter
    assert meta["n_returned"] == 2
    assert meta["tree_truncated"] is True
