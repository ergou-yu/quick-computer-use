"""Unit tests for the shared Quartz keyboard helper.

These exercise the pure parse + keycode lookup surface — no real CGEvent is
posted. Dispatch paths (``type_text`` and ``press_key``) are monkeypatched so
we verify they short-circuit correctly on missing Quartz imports.
"""

from __future__ import annotations

import pytest

from qcu.layers import _keyboard as kb


# ===========================================================================
# parse_key_combo — modifier parsing + primary-key resolution
# ===========================================================================


def test_parse_simple_letter():
    code, mods = kb.parse_key_combo("a")
    assert code == 0  # kVK_ANSI_A
    assert mods == 0


def test_parse_case_insensitive_letter():
    code, _ = kb.parse_key_combo("A")
    assert code == 0


def test_parse_named_key_return():
    code, _ = kb.parse_key_combo("Enter")
    assert code == 36


def test_parse_named_key_escape():
    code, _ = kb.parse_key_combo("Escape")
    assert code == 53


def test_parse_arrow_aliases():
    # "up" and "uparrow" both resolve to keycode 126.
    assert kb.parse_key_combo("Up")[0] == 126
    assert kb.parse_key_combo("UpArrow")[0] == 126


def test_parse_single_modifier_returns_none():
    code, mods = kb.parse_key_combo("Cmd")
    assert code is None


def test_parse_cmd_q():
    code, mods = kb.parse_key_combo("Cmd+Q")
    assert code == 12
    assert mods != 0  # command flag set


def test_parse_cmd_shift_3_two_modifiers():
    code, mods = kb.parse_key_combo("Cmd+Shift+3")
    assert code == 20
    assert mods != 0


def test_parse_ctrl_arrow():
    code, mods = kb.parse_key_combo("Ctrl+Left")
    assert code == 123
    assert mods != 0  # ctrl set


def test_parse_alt_aliases():
    for alias in ("Alt+F4", "Option+F4", "Opt+F4"):
        code, mods = kb.parse_key_combo(alias)
        # F4 isn't in our key table; that's fine — we only assert mod parse.
        assert mods != 0


def test_parse_bracket_for_browser_back():
    code, mods = kb.parse_key_combo("Cmd+[")
    assert code == 33
    assert mods != 0


def test_parse_unresolvable_primary():
    code, _ = kb.parse_key_combo("Cmd+NoSuchKey")
    assert code is None


def test_parse_empty():
    code, _ = kb.parse_key_combo("")
    assert code is None


# ===========================================================================
# KEYCODES table coverage — guard against accidental removals
# ===========================================================================


def test_keycodes_cover_all_letters():
    for letter in "abcdefghijklmnopqrstuvwxyz":
        assert letter in kb.KEYCODES, f"missing letter {letter!r}"


def test_keycodes_cover_symbols():
    for sym in ["[", "]", ",", ".", "-", "=", "/"]:
        assert sym in kb.KEYCODES, f"missing symbol {sym!r}"


def test_keycodes_cover_function_keys():
    for i in range(1, 13):
        assert f"f{i}" in kb.KEYCODES


def test_keycodes_cover_navigation_keys():
    for name in ["return", "tab", "escape", "delete", "space", "home", "end",
                 "pageup", "pagedown", "up", "down", "left", "right"]:
        assert name in kb.KEYCODES, f"missing {name!r}"


# ===========================================================================
# press_key / type_text dispatch — Quartz import simulated
# ===========================================================================


def test_press_key_unknown_primary_returns_false(monkeypatch):
    # Force Quartz import to succeed — the parse failure is what we test.
    monkeypatch.setattr(kb, "press_keycode", lambda code, mods=0: True)
    assert kb.press_key("Cmd+NoSuchKey") is False


def test_press_key_dispatch_success(monkeypatch):
    called = {}

    def fake_press(code, mods=0):
        called["code"] = code
        called["mods"] = mods
        return True

    monkeypatch.setattr(kb, "press_keycode", fake_press)
    assert kb.press_key("Cmd+Q") is True
    assert called["code"] == 12
    assert called["mods"] != 0


def test_press_key_dispatch_failure(monkeypatch):
    monkeypatch.setattr(kb, "press_keycode", lambda code, mods=0: False)
    assert kb.press_key("Escape") is False


def test_type_text_empty_short_circuits():
    # No Quartz import attempted for the trivial case.
    assert kb.type_text("") is True


def test_type_text_quartz_unavailable_returns_false(monkeypatch):
    """Force Quartz import to fail → type_text returns False cleanly."""
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *args, **kw):
        if name == "Quartz" or name.startswith("Quartz."):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert kb.type_text("hello") is False


def test_press_keycode_quartz_unavailable_returns_false(monkeypatch):
    import builtins
    real_import = builtins.__import__

    def _blocked(name, *args, **kw):
        if name == "Quartz" or name.startswith("Quartz."):
            raise ImportError(f"blocked: {name}")
        return real_import(name, *args, **kw)

    monkeypatch.setattr(builtins, "__import__", _blocked)
    assert kb.press_keycode(53) is False