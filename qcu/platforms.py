"""Platform routing and conservative desktop capability reports.

Registration is an extension point, not a claim that a backend is installed or
that the current interactive session can access a particular target.
"""
from __future__ import annotations

import importlib.util
import sys
from typing import Any

_DESKTOP_BACKENDS = {
    "darwin": "desktop_ax", "win32": "desktop_uia",
    "linux": "desktop_linux", "harmony": "desktop_harmony",
}


def platform_name(value: str | None = None) -> str:
    value = value or sys.platform
    if value.startswith("linux"):
        return "linux"
    if value.lower() in {"harmonyos", "openharmony", "harmony"}:
        return "harmony"
    return value


def register_desktop_backend(platform: str, layer: str) -> None:
    """Register a platform selector; register its Layer separately at runtime."""
    if not platform or not layer.startswith("desktop_"):
        raise ValueError("desktop backend requires a platform and desktop_* layer")
    _DESKTOP_BACKENDS[platform_name(platform)] = layer


def desktop_backend_name(platform: str | None = None) -> str:
    return _DESKTOP_BACKENDS.get(platform_name(platform), "desktop_unsupported")


def dependency_exists(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        return False


def desktop_capabilities(*, layer: str | None = None,
                         platform: str | None = None) -> dict[str, Any]:
    """Read-only preflight. Target access stays unknown until a live probe."""
    platform = platform_name(platform)
    layer = layer or desktop_backend_name(platform)
    dependency = {"desktop_ax": "ApplicationServices",
                  "desktop_uia": "uiautomation"}.get(layer)
    intended = {"desktop_ax": "darwin", "desktop_uia": "win32"}.get(layer)
    implemented = layer in {"desktop_ax", "desktop_uia", "desktop_appleevents"}
    deps = dependency_exists(dependency) if dependency else False
    reason = "target_not_probed"
    accessible: bool | None = None
    if not implemented:
        reason, accessible = "backend_not_implemented", False
    elif intended and platform != intended:
        reason, accessible = "platform_mismatch", False
    elif not deps:
        reason, accessible = "dependency_missing", False
    elif layer == "desktop_ax":
        try:
            from ApplicationServices import AXIsProcessTrusted
            if not AXIsProcessTrusted():
                reason, accessible = "accessibility_permission_denied", False
        except Exception as exc:
            reason, accessible = f"dependency_load_failed: {exc}", False
    return {
        "backend": layer, "platform": platform, "implemented": implemented,
        "dependencies_present": deps, "dependency": dependency,
        "session_accessible": accessible, "can_read_controls": None if accessible is None else False,
        "supported_actions": (["click", "invoke", "fill", "toggle", "select"]
                              if layer == "desktop_uia" else
                              ["click", "fill", "press_key", "scroll"] if layer == "desktop_ax" else []),
        "result_verification": implemented and layer in {"desktop_ax", "desktop_uia"},
        "foreground_input_required": False if layer == "desktop_uia" else "action_dependent",
        "available": False, "reason": reason,
        "verification_status": "Windows real device unverified" if layer == "desktop_uia" else None,
    }
