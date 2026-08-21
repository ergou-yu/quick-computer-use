"""macOS TCC permission probes and prompt triggers for the desktop path.

Desktop automation on modern macOS needs **three** independent TCC grants:

- **Accessibility** — `AXUIElement*` and `CGEvent*` (synthesize input).
- **Input Monitoring** — `CGEventPost` / `CGEventCreateKeyboardEvent`
  synthesizing keystrokes. Split out from Accessibility on
  Catalina+/Sequoia+/Tahoe (darwin 23+); granting Accessibility does NOT
  cover it. Probed via `IOKit.IOHIDCheckAccess`.
- **Screen Recording** — `CGWindowListCreateImage`. No official API:
  detected by capturing a region and checking whether it comes back
  empty/all-black. Granted consent only takes effect after the *process*
  that requested it is restarted — see `references/desktop.md`.

This module is layer-agnostic (no desktop_ax imports) so the CLI can call
it during `session start --context desktop` and `qcu doctor` without
constructing a layer. Every probe is defensive: on non-macOS hosts or
when pyobjc is missing, the corresponding entry has ``granted=None`` and
a ``reason`` field instead of raising — that way callers (and the agent
loop) always get a well-formed report.
"""

from __future__ import annotations

import sys
from typing import Any

# Stable macOS "Privacy & Security" deep-link URLs. `open '<url>'` jumps
# straight to the right pane so the user doesn't have to hunt.
_URL_ACCESSIBILITY = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_Accessibility"
)
_URL_INPUT_MONITORING = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_ListenEvent"
)
_URL_SCREEN_RECORDING = (
    "x-apple.systempreferences:com.apple.preference.security"
    "?Privacy_ScreenCapture"
)

# IOKit constants for IOHIDCheckAccess / IOHIDRequestAccess. We load these
# symbols via **ctypes** from /System/Library/Frameworks/IOKit.framework —
# IMPORTANT: there is NO published PyPI wheel for the IOKit PyObjC bindings
# (no such package as `pyobjc-framework-IOKit`), so a ctypes load of the
# system framework is the only dependency-free way to reach them.
# Constants per <IOKit/hid/IOHIDLib.h> / <IOKit/hid/IOHIDTypes.h>:
#   kIOHIDRequestTypeListenEvent = 1   (Input Monitoring)
# Return values of IOHIDCheckAccess (kIOHIDAccessType):
#   kIOHIDAccessTypeGranted = 0
#   kIOHIDAccessTypeDenied  = 1
#   kIOHIDAccessTypeUnknown = 3   (not determined yet)
_KIOHID_REQUEST_TYPE_LISTEN_EVENT = 1
_HID_GRANTED, _HID_DENIED, _HID_UNKNOWN = 0, 1, 3
# Loading the framework once and caching it. Not loaded until first call on
# macOS; stays None on other platforms / if the dlopen fails.
_IOKIT_LIB = None


def _load_iokit():
    """Return a cached ctypes handle to IOKit.framework, or None."""
    global _IOKIT_LIB
    if _IOKIT_LIB is not None:
        return _IOKIT_LIB
    import ctypes  # local import — cheap
    try:
        _IOKIT_LIB = ctypes.cdll.LoadLibrary(
            "/System/Library/Frameworks/IOKit.framework/IOKit"
        )
    except OSError:
        _IOKIT_LIB = False  # mark as tried-and-failed (don't retry)
    return _IOKIT_LIB if _IOKIT_LIB else None


def is_macos() -> bool:
    return sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Individual probes
# ---------------------------------------------------------------------------

def _entry(
    granted: bool | None,
    url: str,
    reason: str | None = None,
) -> dict[str, Any]:
    out: dict[str, Any] = {"granted": granted, "url": url}
    if reason is not None:
        out["reason"] = reason
    return out


def probe_accessibility() -> dict[str, Any]:
    """Accessibility (AXUIElement + CGEvent synthesis)."""
    if not is_macos():
        return _entry(None, _URL_ACCESSIBILITY, reason="not macOS")
    try:
        from ApplicationServices import AXIsProcessTrusted  # type: ignore
    except Exception as e:  # noqa: BLE001 — missing pyobjc extra
        return _entry(None, _URL_ACCESSIBILITY, reason=f"pyobjc unavailable: {e}")
    try:
        return _entry(bool(AXIsProcessTrusted()), _URL_ACCESSIBILITY)
    except Exception as e:  # noqa: BLE001
        return _entry(None, _URL_ACCESSIBILITY, reason=f"{type(e).__name__}: {e}")


def probe_input_monitoring() -> dict[str, Any]:
    """Input Monitoring (CGEventPost keystroke synthesis).

    Probed via ``IOHIDCheckAccess(kIOHIDRequestTypeListenEvent)`` loaded
    straight from the system IOKit framework through ctypes — no PyPI
    package provides these bindings, so ctypes is the only zero-dep path.
    Returns ``granted=None`` when we cannot tell (older macOS without the
    API, or the framework missing); in that case callers fall back to
    attempting the action and relying on the runtime hint in
    ``desktop_ax``.
    """
    if not is_macos():
        return _entry(None, _URL_INPUT_MONITORING, reason="not macOS")
    lib = _load_iokit()
    if lib is None:
        return _entry(
            None, _URL_INPUT_MONITORING, reason="IOKit.framework not loadable"
        )
    try:
        import ctypes

        lib.IOHIDCheckAccess.restype = ctypes.c_int
        lib.IOHIDCheckAccess.argtypes = [ctypes.c_int]
        status = int(lib.IOHIDCheckAccess(_KIOHID_REQUEST_TYPE_LISTEN_EVENT))
        if status == _HID_GRANTED:
            granted = True
        elif status in (_HID_DENIED, _HID_UNKNOWN):
            granted = False
        else:  # unexpected value — don't guess
            return _entry(
                None,
                _URL_INPUT_MONITORING,
                reason=f"unexpected IOHIDCheckAccess value: {status}",
            )
        return _entry(granted, _URL_INPUT_MONITORING)
    except Exception as e:  # noqa: BLE001
        return _entry(None, _URL_INPUT_MONITORING, reason=f"{type(e).__name__}: {e}")


def probe_screen_recording() -> dict[str, Any]:
    """Screen Recording (CGWindowListCreateImage).

    There is no official probe. We capture the on-screen windows and check
    whether the returned image is non-empty. On macOS, when consent is
    denied (or undetermined on first call), ``CGWindowListCreateImage``
    returns either ``None`` or an empty/zero-sized image — never a real
    bitmap. So ``image is not None AND width*height > 0`` is a reliable
    grant indicator, and crucially the *first* call itself surfaces the
    system "Screen Recording" consent dialog (which is exactly what we
    want during preflight).

    We deliberately do NOT decode bitmap pixels: a `CFDataGetBytePtr`
    + pointer-index path segfaulted on real hardware (exit 139) and
    would also mis-read a genuinely all-black workspace as "denied".
    """
    if not is_macos():
        return _entry(None, _URL_SCREEN_RECORDING, reason="not macOS")
    try:
        from Quartz import (  # type: ignore — CoreGraphics symbols route through Quartz
            CGWindowListCreateImage,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            CGRectInfinite,
            CGImageGetWidth,
            CGImageGetHeight,
        )
    except Exception as e:  # noqa: BLE001
        return _entry(None, _URL_SCREEN_RECORDING, reason=f"pyobjc unavailable: {e}")
    try:
        img = CGWindowListCreateImage(
            CGRectInfinite,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
            0,
        )
        if img is None:
            return _entry(False, _URL_SCREEN_RECORDING, reason="image is None")
        w = int(CGImageGetWidth(img))
        h = int(CGImageGetHeight(img))
        granted = w > 0 and h > 0
        return _entry(
            granted,
            _URL_SCREEN_RECORDING,
            reason=None if granted else f"empty capture ({w}x{h})",
        )
    except Exception as e:  # noqa: BLE001
        return _entry(False, _URL_SCREEN_RECORDING, reason=f"{type(e).__name__}: {e}")


def probe_all() -> dict[str, dict[str, Any]]:
    """Probe all three permissions and return a stable-keyed report."""
    return {
        "accessibility": probe_accessibility(),
        "input_monitoring": probe_input_monitoring(),
        "screen_recording": probe_screen_recording(),
    }


# ---------------------------------------------------------------------------
# Prompt triggers (call the macOS consent APIs to surface dialogs)
# ---------------------------------------------------------------------------

def _open_pane(url: str) -> None:
    """Best-effort open a Privacy deep-link in System Settings.

    Uses the same `open` shim a human would; failures are ignored so a
    preflight can never wedge session creation on a UI quirk.
    """
    if not is_macos():
        return
    try:
        import subprocess
        subprocess.run(["open", url], check=False, capture_output=True)
    except Exception:  # noqa: BLE001
        pass


def trigger_prompts_for_missing(report: dict[str, dict[str, Any]]) -> None:
    """Surface the macOS consent dialog for every not-yet-granted item.

    Order matters for UX: pop Accessibility and Input Monitoring first
    (both grant immediately and need no restart), then Screen Recording
    last (grants take effect only on next process launch — the caller
    must tell the user to `qcu session end` + restart).
    """
    if not is_macos():
        return

    acc = report.get("accessibility", {})
    if acc.get("granted") is False:
        try:
            from ApplicationServices import (  # type: ignore
                AXIsProcessTrustedWithOptions,
                kAXTrustedCheckOptionPrompt,
            )
            AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
        except Exception:  # noqa: BLE001
            _open_pane(acc.get("url", _URL_ACCESSIBILITY))

    ime = report.get("input_monitoring", {})
    if ime.get("granted") is False:
        # Same ctypes path as probe_input_monitoring — call IOHIDRequestAccess
        # to surface the consent dialog. Fall back to opening the pane if the
        # framework symbol isn't reachable.
        lib = _load_iokit()
        prompted = False
        if lib is not None:
            try:
                import ctypes

                lib.IOHIDRequestAccess.restype = ctypes.c_int
                lib.IOHIDRequestAccess.argtypes = [ctypes.c_int]
                lib.IOHIDRequestAccess(_KIOHID_REQUEST_TYPE_LISTEN_EVENT)
                prompted = True
            except Exception:  # noqa: BLE001
                pass
        if not prompted:
            _open_pane(ime.get("url", _URL_INPUT_MONITORING))

    scr = report.get("screen_recording", {})
    # Screen Recording: probe_screen_recording already surfaced the dialog
    # if consent was undetermined (the CGWindowListCreateImage call does
    # that). Only force-open the pane when it was explicitly denied.
    if scr.get("granted") is False:
        _open_pane(scr.get("url", _URL_SCREEN_RECORDING))


__all__ = [
    "is_macos",
    "probe_accessibility",
    "probe_input_monitoring",
    "probe_screen_recording",
    "probe_all",
    "trigger_prompts_for_missing",
]
