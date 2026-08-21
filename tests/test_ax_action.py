"""Unit tests for the cross-layer AXPress-over-AppleScript fallback."""

from __future__ import annotations

import subprocess

import pytest

from qcu.layers import _ax_action as mod


# ===========================================================================
# Path-list → AppleScript UI-element chain conversion
# ===========================================================================


def test_path_list_empty_returns_none():
    assert mod.ax_path_list_to_applescript_chain([]) is None


def test_path_list_non_list_returns_none():
    assert mod.ax_path_list_to_applescript_chain("not a list") is None  # type: ignore[arg-type]
    assert mod.ax_path_list_to_applescript_chain(None) is None  # type: ignore[arg-type]


def test_path_list_single_index_one_based():
    # desktop_ax path is 0-based; AppleScript syntax is 1-based.
    assert mod.ax_path_list_to_applescript_chain([0]) == "UI element 1"


def test_path_list_nested():
    # [0, 1, 0] → UI element 1 of UI element 2 of UI element 1
    assert mod.ax_path_list_to_applescript_chain([0, 1, 0]) == (
        "UI element 1 of UI element 2 of UI element 1"
    )


def test_path_list_deep():
    assert mod.ax_path_list_to_applescript_chain([3, 5, 0, 1]) == (
        "UI element 4 of UI element 6 of UI element 1 of UI element 2"
    )


def test_path_list_non_ints_rejected():
    assert mod.ax_path_list_to_applescript_chain(["a", "b"]) is None  # type: ignore[arg-type]
    assert mod.ax_path_list_to_applescript_chain([0, "x", 1]) is None  # type: ignore[arg-type]


# ===========================================================================
# _as_escape — keeps AppleScript string literals safe inside osascript -e
# ===========================================================================


def test_as_escape_quotes_only():
    # Module-specific escape doesn't translate whitespace (callers using
    # process names don't need it).
    assert mod._as_escape('Say "hi"') == 'Say \\"hi\\"'


def test_as_escape_backslashes():
    assert mod._as_escape("a\\b") == "a\\\\b"


def test_as_escape_none():
    assert mod._as_escape(None) == ""


# ===========================================================================
# perform_axpress_via_osascript — subprocess mocking
# ===========================================================================


class _Ok:
    returncode = 0
    stdout = "ok\n"
    stderr = ""


class _Err:
    returncode = 0
    stdout = "err:System Events got an error: Can't get UI element 99.\n"
    stderr = ""


class _ScriptFail:
    returncode = 1
    stdout = ""
    stderr = "-25205 not trusted\n"


def test_perform_axpress_happy_path(monkeypatch):
    """osascript returns 0 and 'ok' → result.ok is True."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Ok())
    ok, msg = mod.perform_axpress_via_osascript(
        "UI element 1 of UI element 2 of process",
        app="Google Chrome",
    )
    assert ok is True
    assert "ok" in msg


def test_perform_axpress_returns_inner_err_string(monkeypatch):
    """``err:`` prefix in stdout is treated as logical failure."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Err())
    ok, msg = mod.perform_axpress_via_osascript(
        "UI element 99 of process", app="Safari",
    )
    assert ok is False
    assert "Can't get UI element 99" in msg


def test_perform_axpress_non_zero_rc(monkeypatch):
    """Non-zero returncode surfaces stderr (truncated)."""
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _ScriptFail())
    ok, msg = mod.perform_axpress_via_osascript(
        "UI element 1 of process", app="Finder",
    )
    assert ok is False
    assert "-25205" in msg


def test_perform_axpress_timeout(monkeypatch):
    """subprocess.TimeoutExpired yields a clear ok=False."""
    def _boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd="osascript", timeout=5.0)
    monkeypatch.setattr(subprocess, "run", _boom)
    ok, msg = mod.perform_axpress_via_osascript("UI element 1", app="Finder")
    assert ok is False
    assert "timed out" in msg


def test_perform_axpress_requires_app_and_chain():
    ok, msg = mod.perform_axpress_via_osascript("", app="Finder")
    assert ok is False
    ok, msg = mod.perform_axpress_via_osascript("UI element 1", app="")
    assert ok is False


def test_perform_axpress_custom_action_name(monkeypatch):
    """``action="AXShowMenu"`` should land in the osascript body."""
    captured = {}

    def fake(args, *rest, **kw):
        # args is the argv list: ["osascript", "-e", "<script>"].
        captured["argv"] = args
        return _Ok()

    monkeypatch.setattr(subprocess, "run", fake)
    mod.perform_axpress_by_attributes(
        app="Chrome", role="AXMenuItem", name="Help", action="AXShowMenu",
    )
    script = " ".join(captured["argv"])
    assert "AXShowMenu" in script


# ===========================================================================
# perform_axpress_by_attributes — the disabled-item guard
# ===========================================================================
#
# This guards the regression ZCode hit: clicking a disabled menu item via
# AppleScript AXPress returns rc=0 from osascript, so the old code reported
# ``ok=True`` even though nothing visible happened. Now the menu-bar fast path
# checks ``enabled of cand`` first and explicitly returns "disabled".


def _fake_run_returning(stdout: str):
    class _R:
        returncode = 0
        # exercise the chosen stdout marker (ok / disabled / none / err:…)
    _R.stdout = stdout + "\n"
    _R.stderr = ""
    return lambda *a, **k: _R()


def test_perform_axpress_disabled_menu_item_is_failure(monkeypatch):
    """When the script returns ``disabled``, the call must yield ok=False."""
    monkeypatch.setattr(subprocess, "run", _fake_run_returning("disabled"))
    ok, msg = mod.perform_axpress_by_attributes(
        app="TextEdit", role="AXMenuItem", name="Save As…",
    )
    assert ok is False
    assert "disabled" in msg
    # Caller gets a hint they can act on — don't retry, surface to user.
    assert "AXPress would no-op" in msg


def test_perform_axpress_menu_item_enabled_returns_ok(monkeypatch):
    """When the script returns ``ok``, the descriptor path yields ok=True."""
    monkeypatch.setattr(subprocess, "run", _fake_run_returning("ok"))
    ok, msg = mod.perform_axpress_by_attributes(
        app="TextEdit", role="AXMenuItem", name="New",
    )
    assert ok is True
    assert "enabled verified" in msg  # the new success marker


def test_perform_axpress_menu_item_not_found_falls_through(monkeypatch):
    """Menu-bar fast-path returns 'none' → fall through to step-2 (whole
    window scan). Step 2's 'none' should also produce a clean failure."""
    monkeypatch.setattr(subprocess, "run", _fake_run_returning("none"))
    ok, msg = mod.perform_axpress_by_attributes(
        app="TextEdit", role="AXMenuItem", name="DoesNotExist",
    )
    # Both steps ran; both said none; final verdict is failure.
    assert ok is False
    assert "no element matched" in msg or "none" in msg


def test_perform_axpress_menu_item_only_step2_used_when_role_not_menu(monkeypatch):
    """When role is not menu-ish, the menu-bar fast path is skipped."""
    captured = []

    def fake(args, *rest, **kw):
        captured.append(" ".join(args))
        return _fake_run_returning("none")()

    monkeypatch.setattr(subprocess, "run", fake)
    mod.perform_axpress_by_attributes(
        app="TextEdit", role="AXButton", name="OK",
    )
    # Exactly one osascript invocation (step 2 only; step 1 was skipped).
    assert len(captured) == 1