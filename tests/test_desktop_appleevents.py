"""Unit tests for the desktop_appleevents layer's pure-logic helpers and the
TSV → Element parsing path. These do NOT fork osascript — they exercise the
code that runs after osascript's output comes back, plus the helpers that
build/ref AppleScript chains.

For live integration tests, mark with ``@pytest.mark.macos`` and run on a
machine where osascript is on PATH.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from qcu.common.types import Action, Rect
from qcu.layers import desktop_appleevents as mod
from qcu.layers.desktop_appleevents import (
    DesktopAppleEventsLayer,
    _as_escape,
    _as_quote,
    _path_to_chain,
)


# ===========================================================================
# AppleScript path → "UI element N of …" chain
# ===========================================================================


def test_path_to_chain_simple_window():
    assert _path_to_chain("w1") == "window 1"


def test_path_to_chain_nested():
    # w1.2.4 → button 4 of toolbar 2 of window 1
    assert _path_to_chain("w1.2.4") == "UI element 4 of UI element 2 of window 1"


def test_path_to_chain_deep():
    assert _path_to_chain("w2.1.3.5") == (
        "UI element 5 of UI element 3 of UI element 1 of window 2"
    )


def test_path_to_chain_process_root_skipped():
    # Process-root walks start with "p"; the segment is ignored, the rest is
    # the chain (so "p.1" becomes "UI element 1 of " — caller rejects empty.)
    assert _path_to_chain("p.1.2") == "UI element 2 of UI element 1 of "


def test_path_to_chain_empty_returns_empty():
    assert _path_to_chain("") == ""
    assert _path_to_chain("p") == ""


def test_path_to_chain_ignores_garbage_segments():
    # Non-numeric segments are skipped without raising.
    assert _path_to_chain("w1.foo.2") == "UI element 2 of window 1"


# ===========================================================================
# AppleScript string escaping
# ===========================================================================


def test_as_escape_plain_string():
    assert _as_escape("hello") == "hello"


def test_as_escape_quotes_and_backslashes():
    assert _as_escape('a"b\\c') == 'a\\"b\\\\c'


def test_as_escape_newline_and_tab():
    assert _as_escape("a\tb\nc") == "a\\tb\\nc"


def test_as_quote_wraps_in_double_quotes():
    assert _as_quote("hi") == '"hi"'


def test_as_quote_escapes_inner_quote():
    assert _as_quote('he said "hi"') == '"he said \\"hi\\""'


# ===========================================================================
# Key-name → keycode lookup (the part press_key needs to send Escape / Tab)
# ===========================================================================


def test_keyname_table_has_required_specials():
    table = mod._KEYNAME_TO_CODE
    assert table["escape"] == 53
    assert table["return"] == 36
    assert table["tab"] == 48
    assert table["space"] == 49
    assert table["delete"] == 51
    assert table["up arrow"] == 126
    assert table["down arrow"] == 125
    assert table["left arrow"] == 123
    assert table["right arrow"] == 124
    assert table["f5"] == 96


# ===========================================================================
# Layer dispatch — purely contractual, no osascript fork
# ===========================================================================


def test_act_rejects_unknown_action_type():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L.act(Action(type="bogus_action", params={}))
    assert r.ok is False
    assert "unsupported action type" in r.message


def test_act_wait_uses_read_only_contract():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L.act(Action(type="wait", params={"ms": 5}))
    assert r.ok is False
    assert r.data["reason"] == "backend_read_only"


def test_act_launch_app_is_read_only():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L.act(Action(type="launch_app", params={}))
    assert r.ok is False
    assert r.data["reason"] == "backend_read_only"


def test_act_activate_app_is_read_only():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L.act(Action(type="activate_app", params={}))
    assert r.ok is False
    assert r.data["reason"] == "backend_read_only"


def test_click_unknown_ref_rejected():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L._click("click", {"ref": "ref_999"})
    assert r.ok is False
    assert "unknown ref" in r.message


def test_fill_known_ref_with_no_path_rejected():
    L = DesktopAppleEventsLayer()
    L._available = True
    # Inject a ref with no resolved positional path.
    L._last_refs["ref_x"] = {"role": "edit", "name": "search", "path": ""}
    r = L._fill({"ref": "ref_x", "text": "hello"})
    assert r.ok is False
    assert "unresolvable" in r.message


def test_resolve_xy_from_params():
    L = DesktopAppleEventsLayer()
    L._available = True
    assert L._resolve_xy({"x": 100, "y": 200}) == (100.0, 200.0)


def test_resolve_xy_from_ref_bounds():
    L = DesktopAppleEventsLayer()
    L._available = True
    L._last_refs["ref_a"] = {"cx": 11.0, "cy": 22.0}
    assert L._resolve_xy({"ref": "ref_a"}) == (11.0, 22.0)


def test_resolve_xy_returns_none_when_nothing_works():
    L = DesktopAppleEventsLayer()
    L._available = True
    assert L._resolve_xy({}) == (None, None)


# ===========================================================================
# observe() parsing — feed the layer a fake osascript result
# ===========================================================================


def _fake_run_factory(tsv: str):
    """Return a fake subprocess.run replacement that yields ``tsv``.

    The same fake serves both the availability probe (``count processes`` →
    some digit string is required) and the walk (any TSV body). It inspects
    the osascript argv to choose which reply to return.
    """
    class _R:
        def __init__(self, stdout, returncode=0, stderr=""):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode
    def fake(args, *rest, **k):
        # Probe shape: ["osascript", "-e", 'tell application "System Events" to count processes']
        argv = " ".join(args)
        if "count processes" in argv:
            return _R("3")
        # Walk shape: ["osascript", "-e", _WALK_SCRIPT, app, depth]
        return _R(tsv)
    return fake


def _make_layer_with_fake_run(monkeypatch, tsv: str, *, app="Finder"):
    """Build a layer pre-armed with ``_available=True`` + a stub probe + walk."""
    L = DesktopAppleEventsLayer()
    L._available = True                              # skip the live probe entirely
    L._osascript = "/usr/bin/osascript"              # don't short-circuit on missing binary
    L._last_app = app
    monkeypatch.setenv("QCU_HOME", "/tmp/qcu-appleevents-tests")
    monkeypatch.setattr(subprocess, "run", _fake_run_factory(tsv))
    # Belt-and-suspenders: short-circuit is_available so prompt-mode doesn't
    # hit subprocess.run with a count-processes shape that looks like TSV.
    monkeypatch.setattr(L, "is_available", lambda *a, **k: True)
    return L


def test_observe_parses_tsv_into_elements(monkeypatch):
    """A well-formed TSV blob produces Element[] with correct bindings."""
    tsv = (
        "AXWindow\tRecents\tstandard window\t\t413\t576\t1721\t1032\t0\tw1\n"
        "AXToolbar\t\ttoolbar\t\t413\t576\t1721\t52\t1\tw1.2\n"
        "AXButton\t\tShare\t\t1613\t576\t41\t52\t2\tw1.2.4\n"
        "AXTextField\t\tsearch text field\t\t1800\t583\t327\t38\t3\tw1.2.8.1\n"
    )
    L = _make_layer_with_fake_run(monkeypatch, tsv)

    obs = L.observe(max_depth=4)

    assert obs.context == "desktop"
    assert obs.url_or_app == "Finder"
    assert obs.title == "Recents"
    # AXToolbar is not interactive → not in elements; AXButton + AXTextField are.
    assert len(obs.elements) == 2
    btn = obs.elements[0]
    assert btn.role == "button"
    # Finder-style toolbar buttons carry their label in AXDescription, not
    # AXTitle — the layer falls back to description so LLMs resolve by label.
    assert btn.name == "Share"
    assert btn.properties.get("description") == "Share"  # raw description kept
    assert btn.bounds == Rect(1613, 576, 41, 52)
    assert btn.backend_id == "w1.2.4"
    assert btn.properties["raw_role"] == "AXButton"

    field = obs.elements[1]
    assert field.role == "edit"  # AXTextField normalized → edit
    assert field.name == "search text field"  # back-filled from description
    assert field.backend_id == "w1.2.8.1"

    # Refs persisted to _last_refs for act() lookups.
    assert btn.ref in L._last_refs
    assert L._last_refs[btn.ref]["path"] == "w1.2.4"


def test_observe_handles_missing_fields_gracefully(monkeypatch):
    """A malformed row (fewer than 10 fields) is skipped, not raised."""
    tsv = "AXButton\tShare\n"  # too few fields
    tsv += "AXButton\tOpen\t\t\t10\t20\t30\t40\t2\tw1.1.1\n"  # valid
    L = _make_layer_with_fake_run(monkeypatch, tsv)

    obs = L.observe(max_depth=4)
    assert len(obs.elements) == 1


def test_observe_no_target_app_returns_empty(monkeypatch):
    """No frontmost app + no prior launch_app → observation says so clearly."""
    L = DesktopAppleEventsLayer()
    monkeypatch.setenv("QCU_HOME", "/tmp/qcu-appleevents-tests")
    monkeypatch.setattr(L, "is_available", lambda *a, **k: True)
    # _target_app_name has no cached value and live probe returns nothing
    monkeypatch.setattr(L, "_target_app_name", lambda: None)
    L._available = True
    L._last_app = None
    obs = L.observe(max_depth=4)
    assert obs.elements == []
    assert "no frontmost" in obs.routing_meta.get("reason", "")


def test_observe_zero_elements_when_osascript_fails(monkeypatch):
    class _Err:
        returncode = 1
        stdout = ""
        stderr = "-1700 some AppleScript error"
    L = DesktopAppleEventsLayer()
    monkeypatch.setenv("QCU_HOME", "/tmp/qcu-appleevents-tests")
    monkeypatch.setattr(L, "is_available", lambda *a, **k: True)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _Err())
    L._available = True
    L._last_app = "Finder"
    obs = L.observe(max_depth=4)
    assert obs.elements == []
    assert "error" in obs.routing_meta


# ===========================================================================
# Press-key: special-key resolution
# ===========================================================================


def test_press_key_missing_key_rejected():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L._press_key("")
    assert r.ok is False
    assert "needs params.key" in r.message


def test_press_key_only_modifier_rejected():
    L = DesktopAppleEventsLayer()
    L._available = True
    r = L._press_key("Cmd+Shift")
    assert r.ok is False
    # New Quartz-based path returns this message; we accept either phrasing
    # since both signal "needs a real primary key".
    assert (
        "needs a non-modifier key" in r.message
        or "not in the keycode table" in r.message
        or "primary key not" in r.message
    )


# ===========================================================================
# Availability guard
# ===========================================================================


def test_is_available_false_when_osascript_missing(monkeypatch):
    L = DesktopAppleEventsLayer()
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    L._osascript = None
    L._available = None
    assert L.is_available() is False


# ===========================================================================
# _probe_web_app — browser tab discovery via native AppleScript dict
# ===========================================================================


class _ProbeResult:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = stderr


def test_probe_web_app_ignores_non_browser_apps():
    """Non-browser app names return (None, '') early — no osascript fork."""
    meta, hint = mod._probe_web_app("Finder")
    assert meta is None
    assert hint == ""
    meta, hint = mod._probe_web_app("TextEdit")
    assert meta is None
    meta, hint = mod._probe_web_app(None)
    assert meta is None


def test_probe_web_app_case_insensitive_browser_match(monkeypatch):
    """Lowercase app names (as System Events sometimes returns) still match."""
    # Avoid actually forking osascript.
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _ProbeResult(stdout="1\thttps://x.com/\tX\n"),
    )
    meta, hint = mod._probe_web_app("google chrome")
    assert meta is not None
    assert meta["browser"] == "google chrome"
    assert len(meta["tabs"]) == 1


def test_probe_web_app_parses_chromium_tsv(monkeypatch):
    """A multi-tab TSV blob from the Chromium-family dict becomes a list."""
    tsv = (
        "1\thttps://example.com/page1\tPage 1\n"
        "2\thttps://example.com/page2\tPage 2\n"
        "3\thttps://example.com/page3\tPage 3\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _ProbeResult(stdout=tsv)
    )
    meta, hint = mod._probe_web_app("Google Chrome")

    assert meta is not None
    assert meta["browser"] == "Google Chrome"
    assert len(meta["tabs"]) == 3
    assert meta["tabs"][0]["url"] == "https://example.com/page1"
    assert meta["tabs"][1]["title"] == "Page 2"
    assert meta["tabs"][2]["index"] == 3

    # Hint mentions browser name + tab count + the actionable suggestion.
    assert "Google Chrome" in hint
    assert "3 open tab" in hint
    assert "navigate the QCU browser" in hint.lower() or "QCU" in hint


def test_probe_web_app_handles_omascript_failure(monkeypatch):
    """When osascript returns non-zero, we return (None, '') — no hint."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: _ProbeResult(returncode=1, stderr="-1700 error"),
    )
    meta, hint = mod._probe_web_app("Google Chrome")
    assert meta is None
    assert hint == ""


def test_probe_web_app_handles_no_tabs(monkeypatch):
    """Browser running but with zero tabs — special-case meta + a 1-line hint."""
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _ProbeResult(stdout="")
    )
    meta, hint = mod._probe_web_app("Google Chrome")
    assert meta is not None
    assert meta["tabs"] == []
    assert "no open tabs" in hint


def test_probe_web_app_skips_malformed_tsv_rows(monkeypatch):
    """Rows with too few fields are dropped, not zeroed-out."""
    tsv = (
        "garbage\n"
        "1\tURL only\n"  # 2 fields — too few
        "2\thttps://ok.com\tOK\n"
    )
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _ProbeResult(stdout=tsv)
    )
    meta, hint = mod._probe_web_app("Google Chrome")
    assert len(meta["tabs"]) == 1
    assert meta["tabs"][0]["url"] == "https://ok.com"


def test_probe_web_app_safari_uses_safeStr_for_missing_value(monkeypatch):
    """Safari sometimes returns the literal string 'missing value' for blank
    tabs or about:blank pages. The new safeStr coercion must convert that
    into an empty string so the row reads 'Untitled / [blank tab]' rather
    than the literal 'missing value' that was leaking into ZCode's view."""
    tsv = "1\t\t[blank tab]\n2\thttps://example.com/\tExample Domain\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _ProbeResult(stdout=tsv)
    )
    meta, hint = mod._probe_web_app("Safari")
    assert meta is not None
    assert meta["browser"] == "Safari"
    assert len(meta["tabs"]) == 2
    # First tab is the blank one — no 'missing value' literal in url.
    blank = meta["tabs"][0]
    assert "missing value" not in blank["url"]
    assert blank["url"] == ""
    # Second tab is the real page; url intact.
    real = meta["tabs"][1]
    assert real["url"] == "https://example.com/"
    assert real["title"] == "Example Domain"


def test_probe_web_app_safari_real_dict_format(monkeypatch):
    """Reproduces the exact TSV the safeStr-wrapped Safari script emits for a
    single loaded tab — make sure parsing keeps working through the new
    wrapper."""
    tsv = "1\thttps://example.com/\tExample Domain\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: _ProbeResult(stdout=tsv)
    )
    meta, hint = mod._probe_web_app("Safari")
    assert meta["tabs"][0]["url"] == "https://example.com/"
    assert meta["tabs"][0]["title"] == "Example Domain"
    assert "Safari" in hint
    assert "1 open tab" in hint


@pytest.mark.parametrize("kind", ["click", "double_click", "fill", "type", "press_key", "scroll"])
def test_public_appleevents_actions_cannot_use_legacy_refs(monkeypatch, kind):
    layer = DesktopAppleEventsLayer()
    layer._last_refs["ref_1"] = {"path": "w1.1", "cx": 10, "cy": 20}
    monkeypatch.setattr(layer, "_run_snippet", lambda *a, **k: pytest.fail("must not dispatch"))
    result = layer.act(Action(kind, {"ref": "ref_1", "x": 10, "y": 20, "text": "test"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "backend_read_only"


@pytest.mark.parametrize("scope", [{"pid": 123}, {"window": "Missing"}, {"window_id": 456}])
def test_appleevents_unsupported_scope_is_not_ignored(monkeypatch, scope):
    layer = DesktopAppleEventsLayer()
    monkeypatch.setattr(layer, "is_available", lambda **k: pytest.fail("must reject before any UI probe"))
    obs = layer.observe(**scope)
    assert obs.routing_meta["reason"] == "unsupported_target_scope"
    assert obs.elements == []
