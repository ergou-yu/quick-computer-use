"""Tests for the desktop_ax layer's logic — key combo parsing, click-type
targeting, and session context switching. These run WITHOUT a real macOS AX
permission grant (they exercise pure parsing / dispatch logic only); the real
AX path is covered by integration tests marked ``@pytest.mark.integration``.
"""

from __future__ import annotations

import os

import pytest

from qcu.common.types import Action
from qcu.layers.desktop_ax import DesktopAXLayer


# ===========================================================================
# Modifier key combo parsing — the bug that broke Cmd+Q / Cmd+Tab / Cmd+W
# ===========================================================================


@pytest.fixture
def layer():
    L = DesktopAXLayer()
    # Tests may run on machines without AX permission (or without pyobjc).
    # Force-availability and trust so the dispatch logic is reachable; the
    # pure-parsing paths don't touch Quartz/AX APIs anyway.
    L._available = True
    L._cached_trusted = True
    return L


def test_parse_cmd_q(layer):
    code, mods = layer._parse_key_combo("Cmd+Q")
    assert code == 12  # kVK_ANSI_Q
    assert mods & (1 << 20)  # command flag set


def test_parse_shift_tab(layer):
    code, mods = layer._parse_key_combo("Shift+Tab")
    assert code == 48  # kVK_Tab
    assert mods & (1 << 17)  # shift flag set
    assert not (mods & (1 << 20))  # no command


def test_parse_cmd_shift_3_two_modifiers(layer):
    code, mods = layer._parse_key_combo("Cmd+Shift+3")
    assert code == 20  # kVK_ANSI_3
    assert mods & (1 << 20)  # cmd
    assert mods & (1 << 17)  # shift


def test_parse_alt_aliases(layer):
    for alias in ("Alt+F4", "Option+F4", "Opt+F4"):
        code, mods = layer._parse_key_combo(alias)
        assert code is not None
        assert mods & (1 << 19)  # alternate flag


def test_parse_ctrl_arrow(layer):
    code, mods = layer._parse_key_combo("Ctrl+Left")
    assert code == 123  # kVK_LeftArrow
    assert mods & (1 << 18)  # control flag


def test_parse_cmd_bracket_for_browser_back(layer):
    # go_back now uses Cmd+[ — verify it resolves to the bracket keycode.
    code, mods = layer._parse_key_combo("Cmd+[")
    assert code == 33  # kVK_ANSI_LeftBracket
    assert mods & (1 << 20)  # cmd


def test_parse_single_letter(layer):
    code, mods = layer._parse_key_combo("a")
    assert code == 0  # kVK_ANSI_A
    assert mods == 0


def test_parse_single_named_key_no_mods(layer):
    code, mods = layer._parse_key_combo("Enter")
    assert code == 36
    assert mods == 0


def test_parse_unresolvable_combo(layer):
    code, mods = layer._parse_key_combo("Cmd+NoSuchKey")
    assert code is None


def test_parse_case_insensitive_command(layer):
    code, mods = layer._parse_key_combo("command+w")
    assert code == 13  # kVK_ANSI_W
    assert mods & (1 << 20)


def test_keycode_table_has_letters_and_symbols(layer):
    layer._ensure_letter_keycodes()
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        assert letter in layer._KEYCODES
    for sym in ("[", "]", ",", ".", "-"):
        assert sym in layer._KEYCODES


# ===========================================================================
# Action dispatch — new desktop actions route correctly
# ===========================================================================


def test_launch_app_requires_app_name(layer):
    r = layer.act(Action(type="launch_app", params={}))
    assert r.ok is False
    assert "app" in r.message


def test_activate_app_requires_app_name(layer):
    r = layer.act(Action(type="activate_app", params={}))
    assert r.ok is False
    assert "app" in r.message


def test_press_key_empty_rejected(layer):
    r = layer._press_key("")
    assert r.ok is False


def test_right_click_routed_to_click_handler(monkeypatch, layer):
    """right_click should dispatch through _click with the right_click type,
    producing a right-button event rather than a left click."""
    seen = {}

    def fake_click(self, atype, params):
        seen["atype"] = atype
        seen["params"] = params
        from qcu.common.types import LayerResult
        return LayerResult(ok=True, layer=self.name, message="fake")

    monkeypatch.setattr(DesktopAXLayer, "_click", fake_click)
    r = layer.act(Action(type="right_click", params={"x": 10, "y": 20}))
    assert r.ok is True
    assert seen["atype"] == "right_click"


def test_double_click_routed_to_click_handler(monkeypatch, layer):
    seen = {}
    def fake_click(self, atype, params):
        seen["atype"] = atype
        from qcu.common.types import LayerResult
        return LayerResult(ok=True, layer=self.name, message="fake")
    monkeypatch.setattr(DesktopAXLayer, "_click", fake_click)
    layer.act(Action(type="double_click", params={"x": 1, "y": 1}))
    assert seen["atype"] == "double_click"


# ===========================================================================
# Session --context switching
# ===========================================================================


def test_session_start_explicit_context_overrides_stale(tmp_path, monkeypatch):
    """`session start --context desktop` after a web session must REPLACE it,
    not silently resume the web layer."""
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    # Avoid importing session too early for QCU_HOME to take effect.
    from qcu import session

    # Simulate a pre-existing web session.
    session.new(context="web", layer="web_a11y")
    assert session.load().context == "web"

    # Replicate the cli_handlers.session_start conflict logic:
    from qcu.session import load, clear

    s = load()
    context = "desktop"
    if s is not None and context != "auto" and s.context != context:
        clear()
        s = None
    assert s is None  # old web session was cleared, allowing a desktop one.

    session.new(context="desktop", layer="desktop_ax")
    assert session.load().context == "desktop"


def test_session_start_same_context_resumes(tmp_path, monkeypatch):
    """Resuming with the same (or auto) context must NOT clear."""
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    from qcu import session

    session.new(context="web", layer="web_a11y")
    sid_before = session.load().session_id

    s = session.load()
    context = "auto"  # default — must not clear
    if s is not None and context != "auto" and s.context != context:
        session.clear()
    assert session.load().session_id == sid_before  # unchanged
