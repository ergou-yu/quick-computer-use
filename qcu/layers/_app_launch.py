"""Shared macOS app-launch helpers.

Both desktop_ax and desktop_appleevents need to start/reuse an app. The
original implementation used ``open -a``, which Forces the app to the
foreground — a usability disaster when QCU is driven by an agent while the
user is actively working. This module exposes a single
:func:`launch_app_background` that prefers:
    1. ``NSWorkspace.launchApplicationAtURL:options:configuration:error:``
       with ``NSWorkspaceLaunchConfigurationActivationKey=False`` — the
       documented "launch without activation" path on macOS 10.15+
    2. AppleScript ``launch application "X"`` — works on cold launches and is
       background-by-default
    3. ``open -a`` as a last-resort fallback (activates; flagged in output)

:func:`activate_app` is kept for the rare caller that genuinely wants focus.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Optional


# Path prefixes that indicate a build/packaging/staging artifact rather than a
# real installed app. When ``mdfind`` returns multiple ``.app`` copies for a
# name (common for apps under active development), these are filtered out so
# ``launch_app("Mirroria")`` opens the real /Applications copy, not a stale
# Xcode DerivedData / ``.build`` / stage directory clone. Field report:
# "按应用名启动命中打包目录同名副本" — the user had to fall back to the
# absolute path /Applications/Mirroria.app.
_BUILD_NOISE_PREFIXES = (
    "/Library/Developer/Xcode/DerivedData",
    "/private/var/folders",
    ".build/",
    "/xcodebuild/",
    "/Xcode/",
    "/xcode/",
    "/stage/",
    "/staging/",
    "/dist/",
    "/build/",
    "DerivedData",
    "xcode-derived",
)


def _is_build_noise(path: str) -> bool:
    """True iff ``path`` looks like a build/packaging artifact, not an installed app."""
    p = path
    for prefix in _BUILD_NOISE_PREFIXES:
        if prefix in p:
            return True
    # Anything under a user's home that isn't /Applications is suspect when a
    # real candidate exists — but we don't hard-reject it, only de-prioritize.
    return False


def _candidate_priority(path: str) -> int:
    """Lower = preferred. Orders candidates so /Applications wins over noise."""
    if path.startswith("/Applications/"):
        return 0
    if path.startswith("/System/Applications/"):
        return 1
    if path.startswith("/System/Library/") or path.startswith("/Library/"):
        return 2
    if "/Applications/" in path:  # nested, e.g. /Volumes/X/Applications
        return 3
    if _is_build_noise(path):
        return 100
    return 50


def _as_escape(s: str) -> str:
    if s is None:
        return ""
    out = []
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\t":
            out.append("\\t")
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        else:
            out.append(ch)
    return "".join(out)


def _app_url_by_name(ws: Any, name: str):
    """Resolve a friendly app name ("Finder", "TextEdit") to a file:// URL.

    Resolution order:
      0. **Absolute path short-circuit**: if ``name`` ends in ``.app`` and is
         an existing directory, use it verbatim. This makes the documented
         "/Applications/Mirroria.app" escape hatch deterministic instead of
         routing through Spotlight (which treats the path as a name string).
      1. Ask System Events for the bundle id of a running app by that name,
         then NSWorkspace.URLForApplicationWithBundleIdentifier_.
      2. mdfind Spotlight for ``.app`` paths whose display name matches.
         Candidates are filtered (build/derived-data noise removed) and sorted
         with a ``/Applications`` preference, so launching by name no longer
         hits a packaging-directory clone when the real app is installed.
    Returns None if none resolve.
    """
    # 0. Absolute path short-circuit — deterministic, no Spotlight ambiguity.
    cand_input = (name or "").strip()
    if cand_input.endswith(".app") and os.path.isdir(cand_input):
        from Foundation import NSURL  # type: ignore

        url = NSURL.fileURLWithPath_(cand_input)
        if url is not None:
            return url

    try:
        res = subprocess.run(
            [
                "osascript",
                "-e",
                (
                    f'tell application "System Events" to return bundle identifier '
                    f'of (first process whose name is "{_as_escape(name)}")'
                ),
            ],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if res.returncode == 0 and res.stdout.strip():
            bundle = res.stdout.strip()
            url = ws.URLForApplicationWithBundleIdentifier_(bundle)
            if url is not None:
                return url
    except Exception:
        pass
    try:
        res = subprocess.run(
            ["mdfind", f"kMDItemKind=='Application' && kMDItemDisplayName=={name!r}"],
            capture_output=True,
            text=True,
            timeout=4,
        )
        # Collect ALL candidates, filter build noise, sort by install priority.
        # Previously this took the FIRST mdfind hit — whose order is unspecified
        # and frequently lands on an Xcode DerivedData / .build clone. See the
        # field report "按应用名启动命中打包目录同名副本".
        candidates: list[str] = []
        for line in (res.stdout or "").splitlines():
            cand = line.strip()
            if cand.endswith(".app") and os.path.exists(cand):
                candidates.append(cand)
        if candidates:
            candidates.sort(key=_candidate_priority)
            best = candidates[0]
            from Foundation import NSURL  # type: ignore

            url = NSURL.fileURLWithPath_(best)
            if url is not None:
                # If multiple non-noise candidates survived, warn — the caller
                # may want to disambiguate with an absolute path next time.
                non_noise = [c for c in candidates if not _is_build_noise(c)]
                if len(non_noise) > 1:
                    print(
                        f"[qcu] app name {name!r} matched multiple installed "
                        f"copies: {non_noise}. Using {best!r}; pass an absolute "
                        f"path to disambiguate.",
                        file=sys.stderr,
                    )
                return url
    except Exception:
        pass
    return None


def launch_app_background(app: str) -> tuple[bool, str, bool]:
    """Launch ``app`` without bringing it to the foreground.

    Returns ``(ok, message, focus_disturbed)``:
      - ``ok`` — whether the launch command itself succeeded
      - ``message`` — short human-readable status for telemetry
      - ``focus_disturbed`` — True only when we degraded to the
        activating fallback (``open -a``). Callers should surface this so
        the user knows QCU grabbed focus.
    """
    app = (app or "").strip()
    if not app:
        return False, "launch_app needs params.app", False

    err_msg = ""
    try:
        from AppKit import NSWorkspace

        ws = NSWorkspace.sharedWorkspace()
        app_url = _app_url_by_name(ws, app)
        if app_url is not None:
            NSWorkspaceLaunchWithoutActivation = 0x00000200
            cfg = {"NSWorkspaceLaunchConfigurationActivationKey": False}
            _ok, _err = ws.launchApplicationAtURL_options_configuration_error_(
                app_url,
                NSWorkspaceLaunchWithoutActivation,
                cfg,
                None,
            )
            if _ok is not None:
                return True, f"launched {app} in background", False
            if _err is not None:
                err_msg = str(_err)[:200]
    except Exception as e:  # noqa: BLE001
        err_msg = f"{type(e).__name__}: {e}"

    # Fallback 1: AppleScript `launch` — background by default, cold-launch only.
    try:
        res = subprocess.run(
            ["osascript", "-e", f'launch application "{_as_escape(app)}"'],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode == 0:
            return True, f"launched {app} via osascript launch", False
    except Exception as e:  # noqa: BLE001
        err_msg = f"launch fallback failed: {e}"

    # Fallback 2: open -a. Activates — flag it.
    try:
        subprocess.Popen(
            ["open", "-a", app],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        time.sleep(0.4)
        return True, f"launched {app} (foreground — background launch failed: {err_msg})", True
    except Exception as e:  # noqa: BLE001
        return False, f"launch_app failed: {type(e).__name__}: {e}", False


def activate_app(app: str) -> tuple[bool, str]:
    """Bring ``app`` to the foreground (rare intended use).

    Returns ``(ok, message)``. Calls ``launch_app_background`` first to make
    sure the app is running, then sends ``activate application`` via osascript.
    """
    app = (app or "").strip()
    if not app:
        return False, "activate_app needs params.app"
    ok, _msg, _ = launch_app_background(app)
    if not ok:
        return False, _msg
    try:
        res = subprocess.run(
            ["osascript", "-e", f'activate application "{_as_escape(app)}"'],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if res.returncode == 0:
            return True, f"activated {app}"
        return False, f"activate failed: {(res.stderr or '').strip()[:200]}"
    except Exception as e:  # noqa: BLE001
        return False, f"activate failed: {type(e).__name__}: {e}"
