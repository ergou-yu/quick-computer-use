"""Tests for the macOS permission probe module.

The probes themselves call into macOS-only APIs (ApplicationServices,
IOKit, CoreGraphics/Quartz). These tests run on any platform and cover:

- shape of the report (always has the 3 keys + url),
- graceful degradation when pyobjc / macOS is unavailable
  (``granted=None`` + ``reason``, never an exception),
- ``trigger_prompts_for_missing`` is a no-op off-macOS,
- the live macOS probes are exercised only under ``@pytest.mark.macos``
  (skipped elsewhere).

We monkeypatch ``is_macos`` plus the relevant import machinery instead
of touching the real TCC subsystem — CI must never trigger a consent
dialog.
"""

from __future__ import annotations

import builtins
import sys
import types

import pytest

from qcu.common import permissions


# ---------------------------------------------------------------------------
# report shape — holds regardless of platform
# ---------------------------------------------------------------------------

def test_probe_all_has_three_keys():
    report = permissions.probe_all()
    assert set(report.keys()) == {
        "accessibility", "input_monitoring", "screen_recording",
    }


def test_every_entry_has_url():
    for entry in permissions.probe_all().values():
        assert "url" in entry
        assert isinstance(entry["url"], str) and entry["url"]


def test_every_entry_has_granted_field():
    for entry in permissions.probe_all().values():
        assert "granted" in entry
        assert entry["granted"] is None or isinstance(entry["granted"], bool)


def test_urls_are_system_settings_deep_links():
    report = permissions.probe_all()
    assert "Privacy_Accessibility" in report["accessibility"]["url"]
    assert "Privacy_ListenEvent" in report["input_monitoring"]["url"]
    assert "Privacy_ScreenCapture" in report["screen_recording"]["url"]


# ---------------------------------------------------------------------------
# graceful degradation — pyobjc / macOS missing
# ---------------------------------------------------------------------------

def test_off_macos_all_granted_none(monkeypatch):
    monkeypatch.setattr(permissions, "is_macos", lambda: False)
    report = permissions.probe_all()
    for entry in report.values():
        assert entry["granted"] is None
        assert entry.get("reason") == "not macOS"


def test_missing_pyobjc_accessibility(monkeypatch):
    """When ApplicationServices can't import, granted=None with a reason."""
    real_import = builtins.__import__

    def _block(name, *args, **kwargs):
        if name == "ApplicationServices":
            raise ImportError("no pyobjc on this host")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    monkeypatch.setattr(builtins, "__import__", _block)
    entry = permissions.probe_accessibility()
    assert entry["granted"] is None
    assert "pyobjc unavailable" in entry["reason"]


def test_missing_iokit_input_monitoring(monkeypatch):
    """When IOKit.framework can't be loaded, granted=None with a reason."""
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    monkeypatch.setattr(permissions, "_load_iokit", lambda: None)
    entry = permissions.probe_input_monitoring()
    assert entry["granted"] is None
    assert "IOKit.framework not loadable" in entry["reason"]


# ---------------------------------------------------------------------------
# probe logic via injected fakes — no real TCC / CG calls
# ---------------------------------------------------------------------------

def _inject_fake_module(monkeypatch, modname, attrs):
    """Register a fake top-level module so `from <modname> import X` works."""
    mod = types.ModuleType(modname)
    for k, v in attrs.items():
        setattr(mod, k, v)
    monkeypatch.setitem(sys.modules, modname, mod)
    return mod


class _FakeIOKitLib:
    """Mimics a ctypes-loaded IOKit.framework with a stubbed status return.

    `set_status` lets a test force what IOHIDCheckAccess returns; restype /
    argtypes calls are ignored (they exist only to mirror the real ctypes
    object's surface so probe_input_monitoring won't trip on them).
    """

    def __init__(self, status: int):
        self._status = status
        self.requested = False

    @property
    def IOHIDCheckAccess(self):
        def _fn(req_type):
            return self._status
        _fn.restype = None
        _fn.argtypes = None
        return _fn

    @property
    def IOHIDRequestAccess(self):
        def _fn(req_type):
            self.requested = True
            return 0
        _fn.restype = None
        _fn.argtypes = None
        return _fn


def test_accessibility_granted_true(monkeypatch):
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    _inject_fake_module(
        monkeypatch, "ApplicationServices", {"AXIsProcessTrusted": lambda: True}
    )
    assert permissions.probe_accessibility()["granted"] is True


def test_accessibility_granted_false(monkeypatch):
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    _inject_fake_module(
        monkeypatch, "ApplicationServices", {"AXIsProcessTrusted": lambda: False}
    )
    assert permissions.probe_accessibility()["granted"] is False


def test_input_monitoring_granted(monkeypatch):
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    monkeypatch.setattr(
        permissions, "_load_iokit", lambda: _FakeIOKitLib(status=0)  # granted
    )
    assert permissions.probe_input_monitoring()["granted"] is True


def test_input_monitoring_denied(monkeypatch):
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    monkeypatch.setattr(
        permissions, "_load_iokit", lambda: _FakeIOKitLib(status=1)  # denied
    )
    assert permissions.probe_input_monitoring()["granted"] is False


def test_input_monitoring_unknown_value_is_unknown(monkeypatch):
    """An unexpected IOHIDCheckAccess return must not be guessed True/False."""
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    monkeypatch.setattr(
        permissions, "_load_iokit", lambda: _FakeIOKitLib(status=999)
    )
    entry = permissions.probe_input_monitoring()
    assert entry["granted"] is None
    assert "unexpected" in entry["reason"]


# ---------------------------------------------------------------------------
# trigger_prompts_for_missing
# ---------------------------------------------------------------------------

def test_trigger_prompts_off_macos_is_noop(monkeypatch):
    """On non-macOS the function must not even attempt imports."""
    monkeypatch.setattr(permissions, "is_macos", lambda: False)
    report = permissions.probe_all()  # all granted=None off-macOS
    # Should return without raising.
    permissions.trigger_prompts_for_missing(report)


def test_trigger_prompts_calls_accessibility_prompt(monkeypatch):
    """A missing Accessibility triggers AXIsProcessTrustedWithOptions(prompt=True)."""
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    called = {}

    def _prompt(opts):
        called["prompt"] = opts

    _inject_fake_module(
        monkeypatch,
        "ApplicationServices",
        {"AXIsProcessTrustedWithOptions": _prompt, "kAXTrustedCheckOptionPrompt": "prompt"},
    )
    # Stub the ctypes-loaded IOKit so trigger_prompts can call IOHIDRequestAccess.
    monkeypatch.setattr(permissions, "_load_iokit", lambda: _FakeIOKitLib(status=1))

    report = {
        "accessibility": {"granted": False, "url": "u://"},
        "input_monitoring": {"granted": False, "url": "u://"},
        "screen_recording": {"granted": True, "url": "u://"},
    }
    permissions.trigger_prompts_for_missing(report)
    assert "prompt" in called  # the missing Accessibility was prompted


def test_trigger_prompts_skips_granted(monkeypatch):
    """Granted permissions are NOT re-prompted."""
    monkeypatch.setattr(permissions, "is_macos", lambda: True)
    called = {"acc": 0, "ime": 0}

    _inject_fake_module(
        monkeypatch,
        "ApplicationServices",
        {"AXIsProcessTrustedWithOptions": lambda _o: called.__setitem__("acc", called["acc"] + 1) or True,
         "kAXTrustedCheckOptionPrompt": "prompt"},
    )
    fake_lib = _FakeIOKitLib(status=0)
    # Track calls on the fake lib instead of through a module.
    monkeypatch.setattr(permissions, "_load_iokit", lambda: fake_lib)

    report = {
        "accessibility": {"granted": True, "url": "u://"},
        "input_monitoring": {"granted": True, "url": "u://"},
        "screen_recording": {"granted": True, "url": "u://"},
    }
    permissions.trigger_prompts_for_missing(report)
    assert called == {"acc": 0, "ime": 0}
    assert fake_lib.requested is False  # granted → IOHIDRequestAccess not called


# ---------------------------------------------------------------------------
# live macOS probe — only on a real mac (skipped elsewhere)
# ---------------------------------------------------------------------------

@pytest.mark.macos
def test_live_probe_all_does_not_raise():
    report = permissions.probe_all()
    for entry in report.values():
        # On a real mac granted must be a concrete bool or None-with-reason,
        # never an unhandled exception (those would have surfaced as test error).
        assert entry["granted"] is None or isinstance(entry["granted"], bool)
