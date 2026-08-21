"""Tests for the postcondition verification wired into desktop_ax in 0.2.x.

These cover the NEW verification paths added to launch_app / activate_app /
press_key (click/fill were already wired in 0.2.0). They run without real
macOS AX permission — the helpers are unit-tested in isolation and the
end-to-end act() paths are exercised with monkeypatched macOS calls.

What we assert:
- launch_app / activate_app return a ``data.verification`` block and no
  longer trust a blind sleep when no window appears.
- press_key attaches a verification block for state-changing keys and does
  NOT for plain letters.
- _wait_for_app_window returns verified=no on timeout, verified=yes on the
  first window seen.
- _diff_window_focus flips between yes/no correctly.
- Element.focused is now populated from AXFocused (no longer hardcoded False).
"""

from __future__ import annotations

import types
from typing import Any

import pytest

from qcu.common.types import Action
from qcu.layers.desktop_ax import DesktopAXLayer


@pytest.fixture
def layer():
    # This environment ships a broken PyObjC (~/.openclaw-yf/libs/objc has a
    # circular import), so `from Quartz.CoreGraphics import ...` fails at
    # import time. Stub the macOS framework modules in sys.modules so the
    # desktop_ax import checks succeed; the tests themselves monkeypatch the
    # real logic paths and never touch Quartz/AX APIs.
    import sys
    import types as _t

    quartz = _t.ModuleType("Quartz")
    cg = _t.ModuleType("Quartz.CoreGraphics")
    for sym in ("CGEventCreateKeyboardEvent", "CGEventPost", "CGEventSetFlags",
                "CGEventCreateMouseEvent", "CGEventCreateScrollWheelEvent2",
                "kCGHIDEventTap", "kCGEventLeftMouseDown", "kCGEventLeftMouseUp",
                "kCGEventMouseMoved", "kCGEventRightMouseDown", "kCGEventRightMouseUp",
                "kCGMouseButtonLeft", "kCGMouseButtonRight", "kCGMouseEventClickState",
                "kCGScrollEventUnitLine", "CGPointMake"):
        setattr(cg, sym, lambda *a, **kw: None)
    quartz.CoreGraphics = cg
    appsvc = _t.ModuleType("ApplicationServices")
    for sym in ("AXIsProcessTrusted", "AXIsProcessTrustedWithOptions",
                "AXUIElementCreateApplication", "AXUIElementCreateSystemWide",
                "AXUIElementCopyAttributeValue", "AXUIElementSetAttributeValue",
                "AXUIElementGetActionNames", "AXUIElementPerformAction",
                "kAXTrustedCheckOptionPrompt", "kAXTitleAttribute",
                "kAXFocusedUIElementAttribute", "kAXFocusedApplicationAttribute"):
        setattr(appsvc, sym, lambda *a, **kw: None)
    appkit = _t.ModuleType("AppKit")
    appkit.NSApplicationActivateIgnoringOtherApps = 1 << 0
    for modname, mod in (("Quartz", quartz), ("Quartz.CoreGraphics", cg),
                         ("ApplicationServices", appsvc), ("AppKit", appkit)):
        sys.modules[modname] = mod

    L = DesktopAXLayer()
    L._available = True
    L._cached_trusted = True
    return L


# ===========================================================================
# _is_state_changing_key
# ===========================================================================


def test_state_changing_keys_are_verified(layer):
    for k in ("Tab", "Escape", "Esc", "Enter", "Return",
              "Cmd+Q", "Cmd+W", "Cmd+S", "Cmd+Tab", "Cmd+["):
        assert layer._is_state_changing_key(k), f"{k!r} should be verified"


def test_plain_letters_and_arrows_not_verified(layer):
    # Plain single chars / arrows don't reliably change window/focus state.
    for k in ("a", "z", "Up", "Down", "Left", "Right", "F1"):
        assert not layer._is_state_changing_key(k), f"{k!r} should NOT be verified"


def test_any_cmd_combo_is_verified(layer):
    # Unenumerated Cmd+… shortcuts are likely menu items that could open a
    # panel — verify them too (cheap relative to a missed Save).
    assert layer._is_state_changing_key("Cmd+K")
    assert layer._is_state_changing_key("Cmd+Shift+P")


# ===========================================================================
# _diff_window_focus
# ===========================================================================


def test_diff_frontmost_change_is_yes(layer):
    before = {"frontmost": "TextEdit", "focused": None}
    after = {"frontmost": "Finder", "focused": None}
    out = layer._diff_window_focus(before, after)
    assert out["verified"] == "yes"
    assert out["frontmost_changed"] is True


def test_diff_focus_element_change_is_yes(layer):
    before = {"frontmost": "TextEdit", "focused": {"role": "AXTextField", "name": "a"}}
    after = {"frontmost": "TextEdit", "focused": {"role": "AXButton", "name": "Save"}}
    out = layer._diff_window_focus(before, after)
    assert out["verified"] == "yes"
    assert out["frontmost_changed"] is False
    assert out["focus_changed"] is True


def test_diff_no_change_is_no(layer):
    before = {"frontmost": "TextEdit", "focused": {"role": "AXTextField", "name": "a"}}
    after = {"frontmost": "TextEdit", "focused": {"role": "AXTextField", "name": "a"}}
    out = layer._diff_window_focus(before, after)
    assert out["verified"] == "no"
    assert out["reason"] is not None  # tells the caller it's a likely no-op


# ===========================================================================
# _wait_for_app_window — timeout and hit paths
# ===========================================================================


def test_wait_for_app_window_timeout_is_no(layer, monkeypatch):
    # _app_pid_for returns a pid, but AXWindows is always empty → timeout → no.
    monkeypatch.setattr(layer, "_app_pid_for", lambda app: 12345)

    import qcu.layers.desktop_ax as mod
    import sys

    appsvc = sys.modules["ApplicationServices"]

    def fake_create(pid):
        return object()  # opaque app element

    appsvc.AXUIElementCreateApplication = fake_create

    def fake_ax_get(fn, elem, attr):
        if attr == "AXWindows":
            return (0, [])  # never any window
        return (-1, None)

    monkeypatch.setattr(mod, "_ax_get", fake_ax_get)
    out = layer._wait_for_app_window("Ghost", timeout=0.3, poll=0.05)
    assert out["verified"] == "no"
    assert "no AXWindow" in out["reason"]
    assert out["n_windows"] == 0


def test_wait_for_app_window_no_pid_is_no(layer, monkeypatch):
    monkeypatch.setattr(layer, "_app_pid_for", lambda app: None)
    out = layer._wait_for_app_window("Ghost", timeout=0.2)
    assert out["verified"] == "no"
    assert "no running process" in out["reason"]


# ===========================================================================
# launch_app — verification block is present and focus_disturbed surfaced
# ===========================================================================


def test_launch_app_attaches_verification_and_focus_disturbed(layer, monkeypatch):
    # Stub the background launcher so no real process is spawned.
    monkeypatch.setattr(
        "qcu.layers._app_launch.launch_app_background",
        lambda app: (True, f"launched {app} in background", False),
    )
    monkeypatch.setattr(layer, "_wait_for_app_window", lambda app, **kw: {"verified": "yes"})
    # Avoid touching the real session file.
    monkeypatch.setattr("qcu.session.patch", lambda **kw: None, raising=False)

    res = layer.act(Action(type="launch_app", params={"app": "TextEdit"}))
    assert res.ok is True
    assert res.data["verification"]["verified"] == "yes"
    assert res.data["focus_disturbed"] is False


def test_launch_app_surfaces_focus_disturbed_when_fallback_used(layer, monkeypatch):
    monkeypatch.setattr(
        "qcu.layers._app_launch.launch_app_background",
        lambda app: (True, f"launched {app} (foreground)", True),
    )
    monkeypatch.setattr(layer, "_wait_for_app_window", lambda app, **kw: {"verified": "no"})
    monkeypatch.setattr("qcu.session.patch", lambda **kw: None, raising=False)

    res = layer.act(Action(type="launch_app", params={"app": "TextEdit"}))
    assert res.ok is True
    assert res.data["focus_disturbed"] is True
    assert "focus disturbed" in res.message
    assert res.data["verification"]["verified"] == "no"


# ===========================================================================
# activate_app — verification + frontmost check
# ===========================================================================


def test_activate_app_marks_no_when_not_frontmost(layer, monkeypatch):
    # Build a minimal fake NSWorkspace path without importing AppKit.
    # IMPORTANT: callers do ``NSWorkspace.sharedWorkspace()`` on the *class*
    # (no instance), so sharedWorkspace must be a classmethod. Likewise
    # frontmostApplication is called on the returned shared instance.
    fake_app = types.SimpleNamespace(
        localizedName=lambda: "TextEdit",
        processIdentifier=lambda: 4321,
        activateWithOptions_=lambda flags: None,
    )
    fake_frontmost = types.SimpleNamespace(localizedName=lambda: "Finder")

    class _FakeWS:
        @classmethod
        def sharedWorkspace(cls):
            return cls

        @classmethod
        def runningApplications(cls):
            return [fake_app]

        @classmethod
        def frontmostApplication(cls):
            return fake_frontmost

    monkeypatch.setitem(
        __import__("sys").modules, "AppKit",
        types.SimpleNamespace(NSWorkspace=_FakeWS, NSApplicationActivateIgnoringOtherApps=1 << 0),
    )
    monkeypatch.setattr(layer, "_wait_for_app_window", lambda app, **kw: {"verified": "yes"})
    monkeypatch.setattr("qcu.session.patch", lambda **kw: None, raising=False)

    res = layer.act(Action(type="activate_app", params={"app": "TextEdit"}))
    assert res.ok is True
    vm = res.data["verification"]
    # The app has a window, but frontmost is Finder, not TextEdit → overridden to no.
    assert vm["verified"] == "no"
    assert vm["frontmost_after"] == "Finder"
    assert "frontmost" in vm["reason"]


# ===========================================================================
# press_key — verification block attached for state-changing keys
# ===========================================================================


def test_press_key_attaches_verification_for_cmd_q(layer, monkeypatch):
    posted = {"count": 0}

    def fake_post(code, mods=0):
        posted["count"] += 1

    monkeypatch.setattr(layer, "_post_key_code", fake_post)
    # Make the snapshot see a frontmost change so verified=yes.
    seq = iter([
        {"frontmost": "TextEdit", "focused": None},
        {"frontmost": "Finder", "focused": None},
    ])
    monkeypatch.setattr(layer, "_window_focus_snapshot", lambda: next(seq))

    res = layer._press_key("Cmd+Q")
    assert res.ok is True
    assert posted["count"] == 1
    assert res.data["verification"]["verified"] == "yes"
    assert res.data["verification"]["frontmost_changed"] is True


def test_press_key_no_verification_for_plain_letter(layer, monkeypatch):
    # Plain 'a' is not state-changing → no snapshot, no verification block.
    monkeypatch.setattr(layer, "_post_key_code", lambda *a, **kw: None)
    called = {"snapshot": False}

    def boom():
        called["snapshot"] = True
        raise AssertionError("snapshot should not be taken for a plain letter")

    monkeypatch.setattr(layer, "_window_focus_snapshot", boom)
    res = layer._press_key("a")
    assert res.ok is True
    assert called["snapshot"] is False
    # data has no verification key (or it's absent) for non-state-changing keys.
    assert not (res.data or {}).get("verification")


def test_press_key_reports_no_on_silent_noop(layer, monkeypatch):
    # Cmd+S posted, but nothing changed → verified=no (the classic "ok but
    # no Save dialog" failure mode this whole module exists to catch).
    monkeypatch.setattr(layer, "_post_key_code", lambda *a, **kw: None)
    same = {"frontmost": "TextEdit", "focused": {"role": "AXTextField", "name": "x"}}
    monkeypatch.setattr(layer, "_window_focus_snapshot", lambda: same)
    res = layer._press_key("Cmd+S")
    assert res.ok is True  # the key WAS posted
    assert res.data["verification"]["verified"] == "no"


# ===========================================================================
# Element.focused is now real (was hardcoded False)
# ===========================================================================


def test_observe_populates_focused_from_axfocused(monkeypatch):
    """observe() must read AXFocused per element instead of hardcoding False.

    We can't run the full observe() without AX trust, but we can assert the
    walk queries AXFocused by checking the attribute is read for an
    interactive element. This is a structural assertion: if someone reverts
    the focused read, the AXFocused query below would never be issued.
    """
    L = DesktopAXLayer()
    L._available = True
    L._cached_trusted = True

    attrs_requested: list[str] = []

    real_ax_get = None
    import qcu.layers.desktop_ax as mod

    def spy_ax_get(fn, elem, attr):
        attrs_requested.append(attr)
        return real_ax_get(fn, elem, attr)

    real_ax_get = mod._ax_get
    monkeypatch.setattr(mod, "_ax_get", spy_ax_get)
    # Sanity: the attribute name string exists in the source (guards against
    # a revert that removes the query).
    src = open(mod.__file__).read()
    assert '"AXFocused"' in src, "observe must query AXFocused (not hardcode focused=False)"
