"""QCU — Quick Computer Use.

A faster computer-use skill that defaults to accessibility-tree observations
and only falls back to screenshots when necessary.
"""

import os as _os
import sys as _sys


def _sanitize_sys_path() -> None:
    """Drop PYTHONPATH entries that bundle an incompatible pyobjc build.

    Why this exists: third-party tool bundles (e.g. OpenClaw's
    ``~/.openclaw-yf/libs``) sometimes export themselves globally via
    ``launchctl setenv PYTHONPATH``. Those bundles vendor pyobjc compiled for a
    specific Python ABI (e.g. ``_objc.cpython-311-darwin.so``). When QCU runs
    under a different interpreter (e.g. 3.13), that shadow ``objc/`` package
    wins over the matching site-packages build, ``import objc`` fails with
    ``cannot import name '_objc'``, the entire macOS Accessibility stack
    (ApplicationServices / Quartz) becomes unimportable, and the router silently
    degrades from the fast ``desktop_ax`` layer (~250 ms, full UI) to the slow
    ``desktop_appleevents`` fallback (~1.5 s, only 3 window buttons).

    We detect the failure mode cheaply — a ``PYTHONPATH`` dir that contains an
    ``objc/`` package whose compiled ``_objc`` extension is not loadable in the
    current interpreter — and remove only that dir from ``sys.path``. Everything
    else on PYTHONPATH is left intact. This is self-healing: no user action,
    reboot, or env-var editing required.
    """
    pyenv = _os.environ.get("PYTHONPATH")
    if not pyenv:
        return

    def _shadows_broken_pyobjc(path: str) -> bool:
        objc_init = _os.path.join(path, "objc", "__init__.py")
        if not _os.path.isfile(objc_init):
            return False
        # Any _objc.*.so present? If none, the bundle isn't vendoring pyobjc.
        objc_dir = _os.path.join(path, "objc")
        try:
            so_names = [n for n in _os.listdir(objc_dir)
                        if n.startswith("_objc.") and n.endswith(".so")]
        except OSError:
            return False
        if not so_names:
            return False
        # If importlib can actually load one, the bundle is fine for us.
        import importlib.machinery as im
        for so in so_names:
            full = _os.path.join(objc_dir, so)
            loader = im.ExtensionFileLoader("objc._objc", full)
            try:
                loader.create_module(spec=None)  # type: ignore[arg-type]
                return False  # loaded fine → not broken for this interpreter
            except Exception:
                continue
        return True  # has _objc .so files but none loadable here

    keep: list[str] = []
    changed = False
    for entry in pyenv.split(_os.pathsep):
        if entry and _shadows_broken_pyobjc(entry):
            changed = True
            # Remove from sys.path too (PYTHONPATH entries are already on it).
            while entry in _sys.path:
                _sys.path.remove(entry)
            continue
        keep.append(entry)

    if changed:
        new_pyenv = _os.pathsep.join(keep)
        if new_pyenv:
            _os.environ["PYTHONPATH"] = new_pyenv
        else:
            _os.environ.pop("PYTHONPATH", None)


# Must run before any pyobjc import (objc / ApplicationServices / Quartz /
# AppKit etc.), so do it at package import time, ahead of the type imports below
# which pull in layers that transitively import pyobjc.
_sanitize_sys_path()

from qcu.common.types import (  # noqa: E402 — intentional after path sanitization
    Action,
    Element,
    LayerResult,
    Observation,
    Rect,
)


def _read_version() -> str:
    """Resolve the installed package version.

    Tries importlib.metadata first (correct on any installed build, wheel or
    editable). Falls back to a literal for source trees without installed
    metadata. Without this, `qcu --version` was stuck on a hard-coded
    "0.1.0" forever, even after bumping pyproject — which made
    "every update ships a new package" misleading.
    """
    try:
        from importlib.metadata import PackageNotFoundError, version
        try:
            return version("qcu")
        except PackageNotFoundError:
            return "0.0.0+unknown"
    except Exception:  # pragma: no cover — extremely defensive
        return "0.0.0+unknown"


__version__ = _read_version()
__all__ = [
    "Action",
    "Element",
    "LayerResult",
    "Observation",
    "Rect",
    "__version__",
]