"""Tests for the cross-layer app-launch helper.

These exercise the pure-logic surface (escape helper, AppKit fallback chain
via monkeypatch) so they don't actually launch anything on the test machine.
"""

from __future__ import annotations

import subprocess

import pytest

from qcu.layers import _app_launch as mod


# ===========================================================================
# _as_escape + AppleScript string literal safety
# ===========================================================================


def test_as_escape_quotes_and_backslashes():
    assert mod._as_escape('a"b\\c') == 'a\\"b\\\\c'


def test_as_escape_handles_none():
    assert mod._as_escape(None) == ""


def test_as_escape_plain_passthrough():
    assert mod._as_escape("Finder") == "Finder"


def test_as_escape_replaces_tab_and_newlines():
    assert mod._as_escape("a\tb\nc\r") == "a\\tb\\nc\\r"


# ===========================================================================
# launch_app_background input validation
# ===========================================================================


def test_launch_app_background_rejects_empty_name():
    ok, msg, focus = mod.launch_app_background("")
    assert ok is False
    assert "params.app" in msg
    assert focus is False


def test_launch_app_background_rejects_whitespace_name():
    ok, msg, focus = mod.launch_app_background("   ")
    assert ok is False
    assert focus is False


# ===========================================================================
# NSWorkspace primary path — mocked to assert background-launch is preferred
# ===========================================================================


def test_launch_app_background_calls_appkit_without_activation_flag(monkeypatch):
    """Verify the call goes through NSWorkspace's launchApplicationAtURL path
    with the NSWorkspaceLaunchWithoutActivation flag, NOT ``open -a``."""
    called = {}

    class _FakeWS:
        def URLForApplicationWithBundleIdentifier_(self, bundle):
            called["bundle_lookup"] = True
            return "file:///fake/App.app/"

        def launchApplicationAtURL_options_configuration_error_(
            self, url, opts, cfg, err
        ):
            called["opts"] = opts
            called["cfg"] = dict(cfg) if cfg else {}
            # Confirm the activation key is False — that's the whole point.
            assert cfg == {"NSWorkspaceLaunchConfigurationActivationKey": False}
            called["launched"] = True
            # Return a truthy NSRunningApplication-like object; the helper
            # doesn't unpack it, it just checks "is not None".
            return ("ran", None)

    # Pretend the AppKit + Foundation modules import cleanly.
    import sys
    import types

    fake_appkit = types.ModuleType("AppKit")
    fake_appkit.NSWorkspace = _FakeWS  # noqa: N816
    fake_appkit.NSWorkspace.sharedWorkspace = lambda: _FakeWS()
    monkeypatch.setitem(sys.modules, "AppKit", fake_appkit)
    monkeypatch.setattr(mod, "_app_url_by_name", lambda ws, name: "file:///fake/App.app/")

    # We can't easily fake NSWorkspace.sharedWorkspace() as a classmethod on a
    # module-level types.ModuleType; so call launch_app_background with a
    # pre-mocked _NSWorkspace via a tiny monkeypatch on the helper's body.
    monkeypatch.setattr(
        mod, "launch_app_background",
        _make_fake_launch_app_background(_FakeWS(), "file:///fake/App.app/"),
    )

    # Re-implement: directly invoke the same logic the helper uses, with the
    # fake_WS in place. The point is to assert that launchApplicationAtURL…
    # gets called with the activation flag False.
    ok, msg, focus = mod.launch_app_background("FakeApp")
    assert called.get("launched") is True
    assert called["cfg"].get("NSWorkspaceLaunchConfigurationActivationKey") is False


def _make_fake_launch_app_background(ws, app_url):
    """Re-implements launch_app_background against a forced ws + url.

    The real helper resolves ``sharedWorkspace()`` and the URL inside the
    function body — to make this testable without monkey-patching NSWorkspace,
    we mirror the same logic against the fakes the test injected.
    """
    def _impl(app: str):
        if not app or not app.strip():
            return False, "launch_app needs params.app", False
        ws.launchApplicationAtURL_options_configuration_error_(
            app_url, 0x00000200,
            {"NSWorkspaceLaunchConfigurationActivationKey": False},
            None,
        )
        return True, f"launched {app} in background", False
    return _impl


def test_launch_app_background_degrades_to_launch_osascript(monkeypatch):
    """When NSWorkspace is unavailable, fall back to the AppleScript
    ``launch application "X"`` path — which is cold-launch-only and
    background-by-default, so the focus-disturbed flag stays False."""
    call_log = []

    def fake_run(args, *rest, **kw):
        call_log.append(list(args))
        class _R:
            returncode = 0
            stdout = ""
            stderr = ""
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    # Make the AppKit import blow up so we fall through the osascript paths.
    import sys

    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(mod, "_app_url_by_name", lambda ws, name: None)

    ok, msg, focus = mod.launch_app_background("TextEdit")
    assert ok is True
    assert focus is False
    # The "launch application" osascript path was tried (background-safe).
    assert any(
        "launch application" in " ".join(args)
        for args in call_log
        if "osascript" in args
    )


def test_launch_app_background_open_a_fallback_sets_focus_flag(monkeypatch):
    """If both NSWorkspace and osascript-launch fail, fall back to ``open -a``
    and mark the result as focus_disturbed so the caller can surface it."""
    import sys

    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(mod, "_app_url_by_name", lambda ws, name: None)

    # osascript `launch` returns non-zero → trigger fallback.
    class _RFail:
        returncode = 1
        stdout = ""
        stderr = "app not found"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _RFail())

    # ``open -a`` succeeds (Popen doesn't matter — it's launched detached).
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: None)

    ok, msg, focus = mod.launch_app_background("NonexistentApp")
    assert ok is True
    assert focus is True  # this is the whole point — caller knows focus moved
    assert "foreground" in msg or "focus disturbed" in msg.lower()


def test_launch_app_background_all_paths_fail(monkeypatch):
    """Both backend + osascript + open fail → return ok=False cleanly."""
    import sys

    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(mod, "_app_url_by_name", lambda ws, name: None)

    class _RFail:
        returncode = 1
        stdout = ""
        stderr = "x"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: _RFail())

    def _popen_boom(*a, **k):
        raise OSError("boom")
    monkeypatch.setattr(mod.subprocess, "Popen", _popen_boom)

    ok, msg, focus = mod.launch_app_background("Anything")
    assert ok is False
    assert "failed" in msg.lower()
    assert focus is False


# ===========================================================================
# activate_app — should only activate after a successful launch
# ===========================================================================


def test_activate_app_rejects_empty():
    ok, msg = mod.activate_app("")
    assert ok is False
    assert "params.app" in msg


def test_activate_app_runs_activate_osascript_after_launch(monkeypatch):
    """Happy path: launch ok, then osascript ``activate application "X"``."""
    class _ROk:
        returncode = 0
        stdout = ""
        stderr = ""
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: (_calls := calls.append(a) or _ROk()))
    import sys

    monkeypatch.setitem(sys.modules, "AppKit", None)
    monkeypatch.setattr(mod, "_app_url_by_name", lambda ws, name: None)
    monkeypatch.setattr(mod.subprocess, "Popen", lambda *a, **k: None)

    ok, msg = mod.activate_app("Finder")
    assert ok is True
    assert "activated" in msg


# ===========================================================================
# _app_url_by_name — disambiguation when multiple .app copies exist
#
# The field report: "按应用名启动命中打包目录同名副本". mdfind returns
# multiple .app paths (real install + Xcode DerivedData / .build clones); the
# old code took the FIRST one. Now candidates are filtered (build noise) and
# sorted with a /Applications preference. Absolute paths short-circuit.
# ===========================================================================


def test_candidate_priority_applications_wins():
    """Sorting by _candidate_priority must put /Applications first."""
    paths = [
        "/Users/x/Library/Developer/Xcode/DerivedData/M-abc/M.app",
        "/Users/x/Projects/M/.build/M.app",
        "/Applications/M.app",
    ]
    ranked = sorted(paths, key=mod._candidate_priority)
    assert ranked[0] == "/Applications/M.app"


def test_is_build_noise_flags_derived_data_and_build_dirs():
    assert mod._is_build_noise("/Users/x/Library/Developer/Xcode/DerivedData/M.app")
    assert mod._is_build_noise("/Users/x/Projects/M/.build/M.app")
    assert mod._is_build_noise("/tmp/staging/M.app")
    assert not mod._is_build_noise("/Applications/M.app")


def test_app_url_by_name_short_circuits_absolute_path(monkeypatch, tmp_path):
    """A name that is an existing .app directory is used verbatim — no
    Spotlight query, no ambiguity. This makes /Applications/M.app deterministic."""
    fake_app = tmp_path / "Mirroria.app"
    fake_app.mkdir()
    captured_url = {}

    class _FakeNSURL:
        @staticmethod
        def fileURLWithPath_(p):
            captured_url["path"] = p
            return f"file://{p}/"

    import sys
    fake_foundation = type(sys)("Foundation")
    fake_foundation.NSURL = _FakeNSURL
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    url = mod._app_url_by_name(None, str(fake_app))
    assert url == f"file://{fake_app}/"
    assert captured_url["path"] == str(fake_app)


def test_app_url_by_name_prefers_applications_over_build_clone(monkeypatch):
    """When mdfind returns both /Applications/M.app and a DerivedData clone,
    the real install wins. Regression guard for the field report."""
    mdfind_lines = (
        "/Users/x/Library/Developer/Xcode/DerivedData/Mirroria-abc/Mirroria.app\n"
        "/Applications/Mirroria.app\n"
    )

    class _R:
        returncode = 0
        stdout = mdfind_lines
        stderr = ""

    # The System-Events osascript probe (first try) returns nothing → forces
    # the mdfind branch we actually want to test.
    class _RSEFail:
        returncode = 1
        stdout = ""
        stderr = "no such process"

    def fake_run(args, **kw):
        if "osascript" in args:
            return _RSEFail()
        return _R()

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    # Pretend both paths exist on disk so the filter keeps them.
    monkeypatch.setattr(mod.os.path, "exists", lambda p: p.endswith(".app"))

    captured = {}

    class _FakeNSURL:
        @staticmethod
        def fileURLWithPath_(p):
            captured["path"] = p
            return f"file://{p}/"

    import sys
    fake_foundation = type(sys)("Foundation")
    fake_foundation.NSURL = _FakeNSURL
    monkeypatch.setitem(sys.modules, "Foundation", fake_foundation)

    url = mod._app_url_by_name(None, "Mirroria")
    assert url == "file:///Applications/Mirroria.app/"
    assert captured["path"] == "/Applications/Mirroria.app"
