"""Unit tests for the AX action-verification engine.

These exercise the pure-logic pieces — signal mapping, hash capture,
normalization, the diff judgment. The live polling loop is parameterized so
unit tests can run it deterministically with a fake ``ax_getter``.
"""

from __future__ import annotations

import time

import pytest

from qcu.layers import _ax_verify as v
from qcu.layers._ax_verify import (
    Snapshot,
    SignalClass,
    Verdict,
    VerdictResult,
    _match_signal,
    _normalize_for_compare,
    hash_string,
    signal_for,
    signal_for_value_set,
    verify,
)


# ===========================================================================
# signal_for — (role, action) → SignalClass mapping
# ===========================================================================


@pytest.mark.parametrize("role,action,expected", [
    ("AXCheckBox", "click", SignalClass.VALUE_FLIP),
    ("AXRadioButton", "click", SignalClass.VALUE_FLIP),
    ("AXSlider", "click", SignalClass.VALUE_DELTA),
    ("AXIncrementor", "increment", SignalClass.VALUE_DELTA),
    ("AXPopUpButton", "click", SignalClass.CHILDREN_CHANGE),
    ("AXMenuItem", "click", SignalClass.MENU_OPEN),
    ("AXMenuItem", "click_quit", SignalClass.ELEMENT_DEATH),
    ("AXMenuItem", "clickclose", SignalClass.ELEMENT_DEATH),
    ("AXButton", "click", SignalClass.BUTTON_PRESS),
    ("unknown_role", "click", SignalClass.BUTTON_PRESS),  # safe fallback
])
def test_signal_for_mapping(role, action, expected):
    assert signal_for(role, action) == expected


def test_signal_for_value_set():
    assert signal_for_value_set("AXTextField") == SignalClass.VALUE_SET


# ===========================================================================
# hash_string + normalize
# ===========================================================================


def test_hash_string_none():
    assert hash_string(None) == (0, "")


def test_hash_string_empty():
    assert hash_string("") == (0, "")


def test_hash_string_stable():
    assert hash_string("hello") == hash_string("hello")


def test_hash_string_distinct():
    a = hash_string("hello")
    b = hash_string("hello!")  # one char extended
    assert a != b  # len differs AND hash differs
    assert a[1] != b[1]


def test_hash_string_catches_tail_diff():
    # 256-char prefix identical, tail differs — must be a different hash
    long_same = "x" * 300
    long_diff = "x" * 300 + "!"
    assert hash_string(long_same) != hash_string(long_diff)


def test_normalize_strips_whitespace():
    assert _normalize_for_compare("  a  b  ") == _normalize_for_compare("ab")


def test_normalize_fullwidth_digits():
    # Full-width ０-９ should normalize to ASCII 0-9.
    assert _normalize_for_compare("全角１２３") == _normalize_for_compare("全角123")


def test_normalize_lowercases():
    assert _normalize_for_compare("AbC") == _normalize_for_compare("abc")


# ===========================================================================
# _match_signal — the diff judgments per signal class
# ===========================================================================


def _snap(**kw):
    return Snapshot(**kw)


def test_match_value_flip_detects_change():
    before = _snap(value_hash=(5, "aaaa"))
    after = _snap(value_hash=(5, "bbbb"))
    assert _match_signal(before, after, SignalClass.VALUE_FLIP, None) is True


def test_match_value_flip_identical_is_no():
    before = _snap(value_hash=(5, "aaaa"))
    after = _snap(value_hash=(5, "aaaa"))
    assert _match_signal(before, after, SignalClass.VALUE_FLIP, None) is False


def test_match_value_delta_numeric():
    before = _snap(value_raw="50")
    after = _snap(value_raw="51")
    assert _match_signal(before, after, SignalClass.VALUE_DELTA, None) is True


def test_match_value_delta_non_numeric_falls_back_to_hash():
    before = _snap(value_raw="abc", value_hash=(3, "1111"))
    after = _snap(value_raw="xyz", value_hash=(3, "2222"))
    assert _match_signal(before, after, SignalClass.VALUE_DELTA, None) is True


def test_match_value_set_strict():
    before = _snap(value_raw="")
    after = _snap(value_raw="hello")
    assert _match_signal(before, after, SignalClass.VALUE_SET, "hello") is True


def test_match_value_set_normalized():
    before = _snap(value_raw="")
    after = _snap(value_raw="  hello  ")
    assert _match_signal(before, after, SignalClass.VALUE_SET, "hello") is True  # whitespace-tolerant


def test_match_value_set_fullwidth_input():
    before = _snap(value_raw="")
    after = _snap(value_raw="abc")
    assert _match_signal(before, after, SignalClass.VALUE_SET,
                        "ａｂｃ") is True  # half/full-width equivalent


def test_match_value_set_rejects_off_by_one():
    before = _snap(value_raw="")
    after = _snap(value_raw="hello!")
    assert _match_signal(before, after, SignalClass.VALUE_SET, "hello") is False


def test_match_element_death():
    before = _snap(element_alive=True)
    after_alive = _snap(element_alive=True)
    after_dead = _snap(element_alive=False)
    assert _match_signal(before, after_alive, SignalClass.ELEMENT_DEATH, None) is False
    assert _match_signal(before, after_dead, SignalClass.ELEMENT_DEATH, None) is True


def test_match_children_change():
    before = _snap(children_count=3)
    after_same = _snap(children_count=3)
    after_more = _snap(children_count=5)
    assert _match_signal(before, after_same, SignalClass.CHILDREN_CHANGE, None) is False
    assert _match_signal(before, after_more, SignalClass.CHILDREN_CHANGE, None) is True


def test_match_window_change():
    before = _snap(window_count=1, window_title_hash=(5, "x"))
    after_a = _snap(window_count=1, window_title_hash=(5, "x"))
    after_b = _snap(window_count=2, window_title_hash=(5, "x"))     # modal added
    after_c = _snap(window_count=1, window_title_hash=(7, "y"))     # title changed
    for after, expected in [(after_a, False), (after_b, True), (after_c, True)]:
        assert _match_signal(before, after, SignalClass.WINDOW_CHANGE, None) is expected


def test_match_menu_open_text_change_counts():
    before = _snap(window_title_hash=(5, "appearance"))
    after = _snap(window_title_hash=(11, "accessibility"))
    assert _match_signal(before, after, SignalClass.MENU_OPEN, None) is True


def test_match_button_press_catches_anything():
    before = _snap(value_hash=(0, ""), children_count=0, window_count=1, focused=False)
    after_no = _snap(value_hash=(0, ""), children_count=0, window_count=1, focused=False)
    assert _match_signal(before, after_no, SignalClass.BUTTON_PRESS, None) is False
    after_any = _snap(focused=True)
    assert _match_signal(before, after_any, SignalClass.BUTTON_PRESS, None) is True


# ===========================================================================
# verify — full polling loop with mocked ax_getter
# ===========================================================================


class _FakeAxGetter:
    """Monkeypatchable ax_getter that returns a scripted sequence of values."""

    def __init__(self, responses):
        # responses is a list per-attribute-name, returned in order
        self._responses = responses

    def __call__(self, fn, elem, attr):
        # We don't use fn; just dispatch on attr.
        seq = self._responses.get(attr, [(0, None)])
        if not seq:
            return (0, None)
        return seq.pop(0) if len(seq) > 0 else (0, None)


def test_verify_returns_na_for_na_signal():
    r = verify(Snapshot(), signal=SignalClass.NA, expected_text=None)
    assert r.verified == Verdict.NA


def test_verify_with_no_ax_getter_returns_na_cleanly():
    r = verify(Snapshot(), signal=SignalClass.VALUE_FLIP, expected_text=None,
               ax_getter=None, target=object())
    assert r.verified == Verdict.NA


def test_verify_early_exit_on_first_match():
    """The signal matches on the first sample → we return within ~one poll.

    No upfront sleep should be paid; ``elapsed_ms`` is small.
    """
    # Sequence: AXValue flips from "0" before to "1" after.
    responses = {
        "AXRole": [(0, "AXCheckBox")],  # element still alive
        "AXValue": [(0, "1")],  # after-area sample returns 1
    }
    ax_getter = _FakeAxGetter(responses)

    # IMPORTANT: before.value_hash must equal what hash_string("0") returns,
    # so the diff against the after-sample is meaningful (and after is "1",
    # which differs from before's "0" hash).
    before = Snapshot(value_hash=hash_string("0"), value_raw="0",
                      timestamp=time.perf_counter())
    t0 = time.perf_counter()
    r = verify(
        before, signal=SignalClass.VALUE_FLIP, ax_getter=ax_getter,
        target=object(), window_ms=2000, first_poll_ms=20, poll_ms=20,
    )
    elapsed = (time.perf_counter() - t0) * 1000
    assert r.verified == Verdict.YES
    assert r.matched_at_ms is not None and r.matched_at_ms < 300  # never near the 2000ms window
    assert elapsed < 500
    assert r.samples_taken == 1


def test_verify_no_match_returns_no_with_samples_count():
    """Window exhausts with no signal → verified=NO, samples_taken > 0."""
    # Same value "x" before and after the action.
    x_hash = hash_string("x")
    # Keep returning "x" for AXValue for enough samples to never match.
    responses = {
        "AXRole": [(0, "AXButton")] * 20,  # always alive
        "AXValue": [(0, "x")] * 20,        # value stays the same
    }
    ax_getter = _FakeAxGetter(responses)
    before = Snapshot(value_hash=x_hash, value_raw="x",
                      timestamp=time.perf_counter())
    r = verify(
        before, signal=SignalClass.VALUE_FLIP, ax_getter=ax_getter,
        target=object(), window_ms=200, first_poll_ms=30, poll_ms=20,
    )
    assert r.verified == Verdict.NO
    assert r.samples_taken >= 2  # we polled multiple times
    assert r.elapsed_ms <= 400  # didn't overrun the window by much


def test_verify_uses_timeout_window_from_signal_class():
    """If caller omits window_ms, we pick from WINDOW_BY_CLASS.

    These numbers are tunables; the test asserts the current tuned values
    so changing them intentionally breaks this test (forcing the engineer
    to acknowledge the change rather than letting it slip unnoticed)."""
    from qcu.layers._ax_verify import WINDOW_BY_CLASS
    # Tuned by data on 2026-08-03 (see _ax_verify.WINDOW_BY_CLASS comment).
    assert WINDOW_BY_CLASS["value_flip"] == 400
    assert WINDOW_BY_CLASS["value_set"] == 400
    assert WINDOW_BY_CLASS["menu_open"] == 2500
    assert WINDOW_BY_CLASS["button_press"] == 1200


def test_verify_element_death_signal_fast_exit():
    """Element disposed within 50ms → verify returns YES for ELEMENT_DEATH."""
    responses = {
        "AXRole": [(0, None)],  # next call after action returns None (dead)
    }
    ax_getter = _FakeAxGetter(responses)
    before = Snapshot(element_alive=True, timestamp=time.perf_counter())
    r = verify(
        before, signal=SignalClass.ELEMENT_DEATH, ax_getter=ax_getter,
        target=object(), window_ms=2000, first_poll_ms=20, poll_ms=20,
    )
    assert r.verified == Verdict.YES
    assert r.matched_at_ms is not None and r.matched_at_ms < 100
    assert r.diagnostics.get("element_disposed") is True


# ===========================================================================
# VerdictResult diagnostics
# ===========================================================================


def test_diagnostics_catches_value_and_children_change():
    before = Snapshot(value_hash=(1, "a"), children_count=3)
    after = Snapshot(value_hash=(1, "b"), children_count=5)
    diag = v._collect_diagnostics(before, after)
    assert diag.get("value_changed") is True
    assert diag.get("children_changed") is True
    assert diag.get("children_delta") == 2
