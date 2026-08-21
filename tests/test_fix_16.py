"""Regression tests for the 1.6 fixes.

1. clean_text must tolerate non-string values (CDP types <input type=number/
   range> values as int, checkboxes as bool) — a raw int previously raised
   TypeError inside re.sub and killed the whole web observe.
2. The AX snapshot/match pair must catch effects that land on a *different*
   element (Calculator keypad → display field) via window_text_hash.
"""

from __future__ import annotations

from qcu.common.normalize import clean_text


class TestCleanTextNonString:
    """Bug: CDP numeric value crashed clean_text (web observe died)."""

    def test_int_value(self):
        # <input type=number value=3> arrives from CDP as int 3
        assert clean_text(3) == "3"

    def test_float_value(self):
        # <input type=number value=2.5> arrives as float
        assert clean_text(2.5) == "2.5"

    def test_bool_true(self):
        # aria-checked arrives as bool
        assert clean_text(True) == "true"

    def test_bool_false(self):
        # falsy bool renders as "false" (checkbox unchecked), not dropped
        assert clean_text(False) == "false"

    def test_int_zero(self):
        # 0 is a legitimate slider/number value, not "empty"
        assert clean_text(0) == "0"

    def test_negative_int(self):
        assert clean_text(-7) == "-7"

    def test_string_passthrough(self):
        assert clean_text("  hello   world  ") == "hello world"

    def test_truncation_still_works(self):
        assert clean_text("ab" * 300, max_len=50).endswith("…")

    def test_none(self):
        assert clean_text(None) == ""


class TestWindowTextHashVerify:
    """Bug: verify=false-negative when the click's effect lands on another
    element (Calculator digit key changes the display, not the button)."""

    @staticmethod
    def _snapshot(text_hash):
        from qcu.layers._ax_verify import Snapshot
        s = Snapshot()
        s.window_text_hash = text_hash
        return s

    def test_button_press_matches_on_window_text_change(self):
        from qcu.layers._ax_verify import SignalClass, _match_signal
        before = self._snapshot((5, "aaa"))
        after = self._snapshot((5, "bbb"))
        assert _match_signal(before, after, SignalClass.BUTTON_PRESS, None) is True

    def test_button_press_still_fails_when_nothing_changed(self):
        from qcu.layers._ax_verify import SignalClass, _match_signal
        before = self._snapshot((5, "aaa"))
        after = self._snapshot((5, "aaa"))
        assert _match_signal(before, after, SignalClass.BUTTON_PRESS, None) is False

    def test_menu_open_matches_on_window_text_change(self):
        from qcu.layers._ax_verify import SignalClass, _match_signal
        before = self._snapshot((5, "aaa"))
        after = self._snapshot((6, "zzz"))
        assert _match_signal(before, after, SignalClass.MENU_OPEN, None) is True
