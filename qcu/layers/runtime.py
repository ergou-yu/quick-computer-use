"""Runtime registry + lazy constructors for layers.

We avoid importing every backend eagerly: ``web_a11y`` needs Playwright,
``desktop_ax`` needs PyObjC, etc. This module picks the right constructor
based on name and only fails if the dependency is missing.
"""

from __future__ import annotations

import threading
from typing import Any, Optional

from qcu.layers.base import Layer


_REGISTRY: dict[str, type[Layer]] = {}
_INSTANCES: dict[str, Layer] = {}
_LOCK = threading.Lock()


def register(name: str) -> Any:
    """Decorator that registers a Layer class by name."""

    def deco(cls: type[Layer]) -> type[Layer]:
        _REGISTRY[name] = cls
        return cls

    return deco


def available() -> list[str]:
    """Return registered/importable adapters, NOT usable runtime capabilities.

    Use capability_report() to distinguish implementation, dependency and access.
    """
    out: list[str] = []
    for name in (
        "web_a11y",
        "webmcp",
        "desktop_ax",
        "desktop_appleevents",
        "desktop_uia",
        "screenshot_fallback",
        "desktop_linux", "desktop_harmony", "desktop_unsupported",
    ):
        try:
            cls = _REGISTRY.get(name)
            if cls is None:
                _try_import(name)
                cls = _REGISTRY.get(name)
            if cls is not None:
                out.append(name)
        except Exception:
            continue
    return out


def get_layer(name: str) -> Layer:
    """Get or create a singleton layer instance by name."""
    with _LOCK:
        inst = _INSTANCES.get(name)
        if inst is not None:
            return inst
        _try_import(name)
        cls = _REGISTRY.get(name)
        if cls is None:
            raise RuntimeError(f"unknown or unloadable layer: {name}")
        inst = cls()
        if getattr(inst, "name", None) == "desktop_unsupported":
            inst.name = name
        _INSTANCES[name] = inst
        return inst


def close_active() -> None:
    """Close all live layer instances. Called by ``qcu session end``."""
    with _LOCK:
        for inst in list(_INSTANCES.values()):
            try:
                inst.close()
            except Exception:
                pass
        _INSTANCES.clear()


def _try_import(name: str) -> None:
    """Lazy-import the layer's module so missing deps don't break unrelated commands."""
    if name in _REGISTRY:
        return
    modname = {
        "web_a11y": "qcu.layers.web_a11y",
        "webmcp": "qcu.layers.webmcp",
        "desktop_ax": "qcu.layers.desktop_ax",
        "desktop_appleevents": "qcu.layers.desktop_appleevents",
        "desktop_uia": "qcu.layers.desktop_uia",
        "screenshot_fallback": "qcu.layers.screenshot_fallback",
        "desktop_linux": "qcu.layers.desktop_unsupported",
        "desktop_harmony": "qcu.layers.desktop_unsupported",
        "desktop_unsupported": "qcu.layers.desktop_unsupported",
    }.get(name)
    if modname is None:
        return
    try:
        __import__(modname)
    except Exception:
        # Leave it unregistered so the caller gets a clear "unknown layer" error.
        pass

def capability_report() -> dict[str, Any]:
    """Inspect desktop capability without claiming that imports imply access."""
    from qcu.platforms import desktop_backend_name, desktop_capabilities
    name = desktop_backend_name()
    instance = _INSTANCES.get(name)
    if instance is not None and hasattr(instance, "capabilities"):
        return instance.capabilities()
    return desktop_capabilities(layer=name)
