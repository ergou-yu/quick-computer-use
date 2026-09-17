"""Read-only macOS Apple Events compatibility observation.

This adapter requires osascript and the applicable Automation permissions.
It can read an application's System Events tree, but cannot bind exact
process/window identities or validate positional paths as actionable refs.
Use desktop_ax observations for native actions. All public ``act`` requests
are refused before dispatch; historical private action helpers remain only
for migration review and are never selected by the runtime action contract.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Any, Optional

from qcu.common.normalize import INTERACTIVE_ROLES, clean_text, from_ax, wait_ms as _wait_ms
from qcu.common.types import Action, Element, LayerResult, Observation, Rect
from qcu.layers.base import Layer
from qcu.layers.runtime import register


# Non-interactive roles we still surface in raw_tree for context (labels,
# window titles). Mirrors desktop_ax._STRUCTURED_ROLES_DESKTOP.
_STRUCTURED_ROLES_DESKTOP = frozenset(
    {"text", "group", "heading", "window", "row", "cell", "list_item", "image"}
)


# Browsers that ship a usable AppleScript dictionary + per-tab URL/title
# (but whose AX web area is normally opaque without AXManualAccessibility).
# Keys are the System Events process names; the value is the AppleScript
# ``tell`` target — usually the same string, except "Google Chrome" maps to
# itself. We try native scripting first and only fall back to leaving the
# hint absent if the dictionary is unreachable.
_WEB_BROWSER_SCRIPTING = {
    "Google Chrome": "Google Chrome",
    "Google Chrome Canary": "Google Chrome Canary",
    "Chromium": "Chromium",
    "Microsoft Edge": "Microsoft Edge",
    "Brave Browser": "Brave Browser",
    "Arc": "Arc",
    "Safari": "Safari",
    "Vivaldi": "Vivaldi",
}


def _probe_web_app(app: Optional[str]) -> tuple[Optional[dict[str, Any]], str]:
    """When ``app`` is a scriptable browser, fetch its tab list natively.

    Returns ``(meta_dict, hint_text)``:
    - ``meta_dict`` goes into ``routing_meta.web_app`` so the LLM sees the
      active URL + title as structured fields. None when the app isn't a
      known browser, isn't running, or its scripting dictionary refused.
    - ``hint_text`` is a short raw_tree preface telling the LLM why the AX
      tree looks thin and what to do (navigate QCU's own browser to the
      same URL to operate on the DOM).

    Why: Chrome / Safari / Edge only expose their chrome to the AX tree (the
    traffic-light buttons + omnibox) without exposing the DOM. Knowing the
    current URL is enough for an LLM to decide "I should re-open this page in
    my own Chromium via CDP". This avoids the "我只看到窗口标题" dead-end.
    """
    if not isinstance(app, str):
        return None, ""
    target = _WEB_BROWSER_SCRIPTING.get(app)
    if target is None:
        # Case-insensitive last-ditch match — "google chrome" is also valid.
        for k, v in _WEB_BROWSER_SCRIPTING.items():
            if app.lower() == k.lower():
                target = v
                break
        if target is None:
            return None, ""

    try:
        if target == "Safari":
            # Safari's AppleScript model: windows contain a single document
            # (the active tab). ``document of w`` may exist even when no
            # real page is loaded (about:blank, blank tab), in which case
            # ``URL of d`` returns literal ``missing value`` rather than
            # raising — that's what surfaced as "url: missing value" in
            # ZCode's testing. We coerce both fields to text, treat the
            # string "missing value" as empty, and skip rows where neither
            # URL nor title resolves.
            script = (
                f'on safeStr(x)\n'
                f"  try\n"
                f"    set s to x as text\n"
                f'    if s is "missing value" then return ""\n'
                f"    return s\n"
                f"  on error\n"
                f'    return ""\n'
                f"  end try\n"
                f"end safeStr\n"
                f'\n'
                f'tell application "Safari"\n'
                f"  set outp to \"\"\n"
                f"  set i to 0\n"
                f"  repeat with w in windows\n"
                f"    try\n"
                f"      set d to document of w\n"
                f"      set i to i + 1\n"
                f"      set u to my safeStr(URL of d)\n"
                f"      set n to my safeStr(name of d)\n"
                f"      if u is not \"\" or n is not \"\" then\n"
                f'        set outp to outp & (i as text) & "\\t" & u & "\\t" & n & linefeed\n'
                f"      else\n"
                f'        set outp to outp & (i as text) & "\\t\\t[blank tab]" & linefeed\n'
                f"      end if\n"
                f"    end try\n"
                f"  end repeat\n"
                f"  return outp\n"
                f"end tell"
            )
        else:
            # Chromium-family tab model: windows contain tabs, tabs have URL,
            # title, and loading. Use the same TSV format. Also coerce missing
            # value to "" so a partially-loaded tab doesn't pollute the row.
            script = (
                f'on safeStr(x)\n'
                f"  try\n"
                f"    set s to x as text\n"
                f'    if s is "missing value" then return ""\n'
                f"    return s\n"
                f"  on error\n"
                f'    return ""\n'
                f"  end try\n"
                f"end safeStr\n"
                f'\n'
                f'tell application "{target}"\n'
                f"  set outp to \"\"\n"
                f"  set i to 0\n"
                f"  repeat with w in windows\n"
                f"    repeat with t in tabs of w\n"
                f"      set i to i + 1\n"
                f"      set u to my safeStr(URL of t)\n"
                f"      set n to my safeStr(title of t)\n"
                f'      set outp to outp & (i as text) & "\\t" & u & "\\t" & n & linefeed\n'
                f"    end repeat\n"
                f"  end repeat\n"
                f"  return outp\n"
                f"end tell"
            )
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=4,
        )
    except Exception:
        return None, ""
    if res.returncode != 0:
        return None, ""
    tabs: list[dict[str, Any]] = []
    for ln in (res.stdout or "").splitlines():
        if not ln.strip():
            continue
        parts = ln.split("\t")
        if len(parts) < 3:
            continue
        try:
            idx = int(parts[0])
        except ValueError:
            continue
        url = parts[1] or ""
        title = parts[2] or ""
        if url or title:
            tabs.append({"index": idx, "url": url, "title": title})

    if not tabs:
        # Browser has no open tabs (or dictionary returned nothing).
        return (
            {"browser": app, "tabs": []},
            f"# {app} — no open tabs (or scripting dictionary empty)",
        )

    # Build a hint block for raw_tree. The LLM-friendly format is one tab per
    # line, marked with [active] on the most recent one we can detect (window 1,
    # active tab — we can't always tell from osascript which is "current"; this
    # is good enough for the LLM).
    lines = [f"# {app} — {len(tabs)} open tab(s). AX only sees the chrome; DOM is opaque."]
    lines.append("# To act on a page's DOM, navigate the QCU browser to the same URL:")
    for t in tabs:
        url = t["url"]
        ttitle = t["title"]
        active = " [window 1 tab 1]" if t["index"] == 1 else ""
        lines.append(f"#   [{t['index']:>2}] {ttitle}{active}")
        lines.append(f"#        url: {url}")
    hint = "\n".join(lines)
    return {"browser": app, "tabs": tabs}, hint


# AppleScript virtual keycodes (subset of Carbon/HIToolbox/Events.h kVK_*).
# Used by ``key code N`` — the only reliable way to emit non-printable keys
# (arrows, function keys, Escape, etc.) via ``osascript``; ``keystroke``
# treats brace tokens as record literals and fails in subprocess context.
# Keep in sync with desktop_ax._KEYCODES for the names we both need.
_KEYNAME_TO_CODE: dict[str, int] = {
    "return": 36,
    "tab": 48,
    "space": 49,
    "delete": 51,
    "escape": 53,
    "forward delete": 117,
    "home": 115,
    "end": 119,
    "page up": 116,
    "page down": 121,
    "up arrow": 126,
    "down arrow": 125,
    "left arrow": 123,
    "right arrow": 124,
    "help": 114,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}


# ---------------------------------------------------------------------------
# AppleScript template. Walking is done in-AppleScript for speed; the output
# is a TSV one element per line, fields:
#   role \t name \t description \t value \t x \t y \t w \t h \t depth \t path
# ``path`` is a dot-chain "w1.2.4" that we expand to "UI element 4 of UI
# element 2 of window 1" when re-targeting during act().
# The osascript process exits with this string on stdout.
# ---------------------------------------------------------------------------

_WALK_SCRIPT = r"""
-- Recursively walk a UI element tree, emitting one TSV line per node.
-- Caller supplies the target process name and max depth via stdin lines.
-- Helpers MUST live at top level (outside `on run`) — nested `on` defs are
-- a syntax error inside `on run` on stock osascript.

on replace(str, what, with_)
    set oldDelim to AppleScript's text item delimiters
    set AppleScript's text item delimiters to what
    set parts to text items of str
    set AppleScript's text item delimiters to with_
    set outt to parts as text
    set AppleScript's text item delimiters to oldDelim
    return outt
end replace

on txt(x)
    -- Coerce anything to a string. ``missing value`` coerces to the literal
    -- "missing value", so we treat that as an empty string explicitly — the
    -- code on the Python side relies on empty strings for absent values.
    try
        set s to x as text
        if s is "missing value" then return ""
        return s
    on error
        return ""
    end try
end txt

on safeTab(s)
    set s to my txt(s)
    set s to my replace(s, tab, " ")
    return my replace(s, linefeed, " ")
end safeTab

on emit(role, name, dsc, val, px, py, sw, sh, depth, pth)
    return (my safeTab(role)) & tab & (my safeTab(name)) & tab & (my safeTab(dsc)) & tab & (my safeTab(val)) & tab & (my safeTab(px)) & tab & (my safeTab(py)) & tab & (my safeTab(sw)) & tab & (my safeTab(sh)) & tab & (depth as text) & tab & pth
end emit

on walk(elem, depth, pth, appName, maxDepth, refOut)
    tell application "System Events"
        tell process appName
            try
                set r to role of elem
            on error
                set r to ""
            end try
            try
                set n to name of elem
            on error
                set n to ""
            end try
            try
                set d to description of elem
            on error
                set d to ""
            end try
            try
                set v to value of elem
            on error
                set v to ""
            end try
            try
                set p to position of elem
                set px to item 1 of p
                set py to item 2 of p
            on error
                set px to 0
                set py to 0
            end try
            try
                set s to size of elem
                set sw to item 1 of s
                set sh to item 2 of s
            on error
                set sw to 0
                set sh to 0
            end try
            set rec to my emit(r, n, d, v, px, py, sw, sh, depth, pth)
            set contents of refOut to (contents of refOut) & rec & linefeed
            try
                set kids to UI elements of elem
                set kCount to count of kids
            on error
                set kCount to 0
            end try
            if kCount > 0 and depth < maxDepth then
                repeat with i from 1 to kCount
                    my walk(item i of kids, depth + 1, pth & "." & i, appName, maxDepth, refOut)
                end repeat
            end if
        end tell
    end tell
end walk

on run argv
    set appName to item 1 of argv
    set maxDepth to (item 2 of argv) as integer

    tell application "System Events"
        tell process appName
            set winCount to 0
            try
                set winCount to count of windows
            end try

            if winCount = 0 then
                set out to ""
                my walk(application process appName, 0, "p", appName, maxDepth, (a reference to out))
                return out
            end if

            set out to ""
            repeat with wi from 1 to winCount
                my walk(item wi of windows, 0, "w" & wi, appName, maxDepth, (a reference to out))
            end repeat
            return out
        end tell
    end tell
end run
"""


# Single-line action snippets. Each is wrapped in `tell process NAME` so the
# evaluator doesn't have to repeat that boilerplate. Argument escaping is done
# in Python to dodge AppleScript string interpolation pitfalls.


def _as_escape(s: str) -> str:
    """Escape a string for an AppleScript double-quoted literal."""
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


def _path_to_chain(path: str) -> str:
    """Convert a path string like "w1.2.4" into the AppleScript chain
    ``UI element 4 of UI element 2 of window 1``. ``p`` prefix means the walk
    started from the process root (no window); kept for completeness."""
    parts = path.split(".")
    body = ""
    for part in parts:
        if not part:
            continue
        if part[0] == "w":
            try:
                idx = int(part[1:])
            except ValueError:
                continue
            body = f"window {idx}"
        elif part[0] == "p":
            # process root: append children of the process itself.
            continue
        else:
            try:
                idx = int(part)
            except ValueError:
                continue
            body = f"UI element {idx} of {body}"
    if body == "":
        # Process-root walk with no sub path → can't target the process element
        # itself for a click; callers should reject this.
        return ""
    return body


@register("desktop_appleevents")
class DesktopAppleEventsLayer(Layer):
    name = "desktop_appleevents"

    def __init__(self) -> None:
        self._osascript: Optional[str] = shutil.which("osascript")
        # Cache of the last successful observe: ref -> element metadata.
        # Survives only in this process; cross-process ref persistence goes
        # through session.last_refs, same shape desktop_ax uses.
        self._last_refs: dict[str, dict[str, Any]] = {}
        self._last_app: Optional[str] = None
        self._last_title: Optional[str] = None
        self._available: Optional[bool] = None

        # Restore cached refs from session so a fresh ``qcu act`` process can
        # still click refs observed by a prior process. The *act* path re-runs
        # no osascript for resolution; it trusts the cached path string.
        try:
            from qcu.session import load as _load

            s = _load()
            if s and s.last_refs:
                for entry in s.last_refs:
                    ref = entry.get("ref")
                    if not ref:
                        continue
                    b = entry.get("bounds")
                    bounds = None
                    if b:
                        bounds = Rect(b["x"], b["y"], b["width"], b["height"])
                    self._last_refs[ref] = {
                        "role": entry.get("role"),
                        "role_raw": entry.get("role_raw"),
                        "name": entry.get("name"),
                        "value": entry.get("value"),
                        "path": entry.get("path"),
                        "cx": entry.get("cx"),
                        "cy": entry.get("cy"),
                        "bounds": bounds,
                    }
            if s and s.target_app:
                self._last_app = s.target_app
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    def is_available(self, *, prompt: bool = False) -> bool:
        """Return True if osascript + Apple Events work for System Events.

        We treat this as the proxy for "Automation permission granted". First
        call runs a tiny osascript to be sure; the result is cached on the
        instance. ``prompt=True`` forces a re-probe (useful after the user
        may have toggled permission in System Settings between calls).
        """
        if self._osascript is None:
            self._available = False
            return False
        if self._available is not None and not prompt:
            return self._available
        try:
            res = subprocess.run(
                ["osascript", "-e", 'tell application "System Events" to count processes'],
                capture_output=True,
                text=True,
                timeout=5,
            )
            ok = res.returncode == 0 and res.stdout.strip().isdigit()
        except Exception:
            ok = False
        self._available = bool(ok)
        return self._available

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 6, **options: Any) -> Observation:
        """Run the AppleScript walk and parse its TSV output into Elements."""
        unsupported = {key: options[key] for key in ("pid", "window", "window_id") if options.get(key) is not None}
        if unsupported:
            return Observation(context="desktop", elements=[], routing_meta={
                "layer": self.name, "available": False, "reason": "unsupported_target_scope",
                "read_only": True, "requested": unsupported,
                "error": "Apple Events compatibility observation cannot bind a process/window; use desktop_ax"})
        if options.get("app"):
            self._last_app = str(options["app"])
        if not self.is_available(prompt=True):
            return Observation(
                context="desktop",
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "available": False,
                    "reason": "osascript unavailable or Automation permission not granted",
                },
                # raw_tree preface so the agent doesn't misread an empty element
                # list as "the app has no UI". This is the Apple Events (Automation)
                # permission gap, not an empty app — see the Kimi field report.
                raw_tree=(
                    "# ⚠ 無法讀取 macOS UI:Automation(Apple Events)權限未授予,或 osascript 不可用。\n"
                    "# 這不是「應用沒有介面」——元素清單是空的只是因為 QCU 沒有 Automation 權限。\n"
                    "# 補救步驟:\n"
                    "#   1. 執行 `qcu doctor` 確認權限狀態\n"
                    "#   2. 到 系統設定 > 隱私權與安全性 > 自動化,允許 QCU 控制 System Events\n"
                    "#      (或 `open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Automation'`)\n"
                    "#   3. 授權後 `qcu session end && qcu session start --context desktop` 重啟\n"
                    "# Automation/Apple Events permission not granted — empty elements = permission\n"
                    "# gap, not an empty UI. Run `qcu doctor`, grant in System Settings > Privacy &\n"
                    "# Security > Automation, then restart the desktop session."
                ),
            )

        app = self._target_app_name()
        if app is None:
            return Observation(
                context="desktop",
                url_or_app=None,
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "available": True,
                    "reason": "no frontmost application; activate or launch_app first",
                },
                # Scoping gap, not a permission gap: the agent asked to observe
                # but no app is focused / the target_app was never set. Tell it
                # the concrete fix instead of leaving an empty tree.
                raw_tree=(
                    "# ⚠ 沒有可觀察的前台應用。\n"
                    "# 先 `launch_app` 或 `activate_app` 指定目標,或在 observe 加 `--app <名稱>`。\n"
                    "# No frontmost application to observe. Use `launch_app` / `activate_app`\n"
                    "# first, or pass `--app <name>` / `--pid <n>` to scope observe to a target."
                ),
            )

        # Run the walk. If the process owns zero windows we still emit a
        # process-root walk via the script.
        # Cap the effective depth at 4: Apple Events round-trips every
        # attribute (role/name/value/position/size) for every node, so depth
        # scales linearly with per-IPC cost — Finder / System Settings can be
        # 1000+ nodes at depth ≥6 (15+s wall, our timeout). Depth 4 covers
        # ~all interactive elements on stock macOS apps (window → content
        # group → toolbar/pane → button), AND keeps the JSON <3KB so the LLM
        # gets a tight, focused snapshot. If a target is deeper than 4 the
        # LLM can navigate through menus (which surface at low depth).
        eff_depth = min(max_depth if max_depth > 0 else 4, 4)
        try:
            res = subprocess.run(
                ["osascript", "-e", _WALK_SCRIPT, app, str(eff_depth)],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            return Observation(
                context="desktop",
                url_or_app=app,
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "error": "osascript walk timed out (>15s); tree too deep or app wedged",
                },
            )
        if res.returncode != 0:
            return Observation(
                context="desktop",
                url_or_app=app,
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "error": (res.stderr or "").strip()[:500] or "osascript returned non-zero",
                },
            )

        elements: list[Element] = []
        raw_lines: list[str] = []
        new_refs: dict[str, dict[str, Any]] = {}
        counter = 0
        title: Optional[str] = None
        # Detect an embedded WKWebView/Electron web view (AXWebArea). Same
        # rationale as desktop_ax.observe: when the app content is an opaque
        # web view, the Apple Events walk sees only the shell and we must
        # hint the LLM toward the T3 web-takeover path. No URL is recoverable
        # from a WKWebView app's SE dictionary in general.
        found_web_area: bool = False

        for raw_line in (res.stdout or "").splitlines():
            if not raw_line.strip():
                continue
            fields = raw_line.split("\t")
            # Defensive: tolerate rows where value/name field swallowed a tab.
            if len(fields) < 10:
                continue
            role_raw = fields[0]
            name = fields[1]
            description = fields[2]
            value = fields[3]
            try:
                x = float(fields[4])
                y = float(fields[5])
                w = float(fields[6])
                h = float(fields[7])
                depth_i = int(fields[8])
            except ValueError:
                continue
            path = fields[9]

            role_norm = from_ax(role_raw)
            name_s = clean_text(name)
            value_s = clean_text(value)
            description_s = clean_text(description)

            # Flag an embedded web view. role_raw is the raw AX role string
            # from the TSV walk; "AXWebArea" is the unique discriminator
            # (normalize maps it to the generic "document").
            if role_raw == "AXWebArea":
                found_web_area = True

            # macOS often leaves AXTitle empty and puts the user-visible label
            # in AXDescription (Finder toolbar buttons, search fields, lots of
            # menu-only items). Surface description as a name fallback so the
            # LLM can resolve elements by label without digging into
            # properties. The raw description is also kept on properties.
            if not name_s and description_s:
                name_s = description_s

            # Capture the first window's name as the page title for parity
            # with web_a11y.observe.
            if title is None and path == "w1":
                title = name_s or None

            bounds: Optional[Rect] = None
            cx: Optional[float] = None
            cy: Optional[float] = None
            if w > 0 and h > 0:
                bounds = Rect(x, y, w, h)
                cx = bounds.cx
                cy = bounds.cy

            # Interactive elements get a ref and surface in elements[].
            if role_norm in INTERACTIVE_ROLES:
                ref = f"ref_{counter}"
                counter += 1
                el = Element(
                    ref=ref,
                    role=role_norm,
                    name=name_s or None,
                    value=value_s or None,
                    enabled=True,  # enabled isn't reliably exposed via SE
                    focused=False,
                    bounds=bounds,
                    backend_id=path,
                    properties={
                        "raw_role": role_raw,
                        "description": clean_text(description),
                        "depth": depth_i,
                    },
                )
                elements.append(el)
                new_refs[ref] = {
                    "role": role_norm,
                    "role_raw": role_raw,
                    "name": name_s,
                    "value": value_s,
                    "path": path,
                    "cx": cx,
                    "cy": cy,
                    "bounds": bounds,
                }
                indent = "  " * depth_i
                line = f"{indent}[{ref}] {role_norm}"
                if name_s:
                    line += f" {name_s!r}"
                if value_s:
                    line += f" = {value_s!r}"
                raw_lines.append(line)
            elif name_s and role_norm in _STRUCTURED_ROLES_DESKTOP:
                indent = "  " * depth_i
                raw_lines.append(f"{indent}{role_norm} {name_s!r}")

        for element in elements:
            element.properties["actionable"] = False
            element.properties["read_only_backend"] = True
        self._last_refs = new_refs
        self._last_app = app
        self._last_title = title

        # Persist refs so a fresh CLI invocation (``qcu act``) can resolve
        # refs without re-observing.
        try:
            from qcu.session import patch

            patch(
                last_refs=_serialize_refs(new_refs),
                target_app=app,
                context="desktop",
                layer=self.name,
            )
        except Exception:
            pass

        # Web-app awareness: when the target is Chrome / Safari / Edge / Brave,
        # the AX tree only shows the chrome (window frame) — DOM is opaque. The
        # browser's native AppleScript dictionary DOES expose per-tab URL and
        # title cheaply (and JS if the user enabled "Allow JavaScript from
        # Apple Events"). We fetch those so the LLM at least knows WHAT page
        # is open, and we surface a hint that to act on the DOM it should
        # navigate QCU's own headless Chromium to the same URL.
        web_app_meta, web_app_hint = _probe_web_app(app)

        # Pagination + filter + compact. Same options web_a11y uses
        # (``--limit``, ``--offset``, ``--role``, ``--name``, ``--compact``).
        # Important: the *full* _last_refs is preserved so act() can still
        # resolve ANY ref the LLM picks — pagination only changes what
        # surface elements[] and raw_tree carry back to the caller, not what
        # is clickable in this observe's session.
        from qcu.layers._paginate import apply_pagination_and_compact

        sliced, page_meta = apply_pagination_and_compact(
            elements,
            limit=options.get("limit"),
            offset=int(options.get("offset") or 0),
            compact=bool(options.get("compact", False)),
            role_filter=options.get("roles"),  # CLI flag is --role; cli_handlers renames
            name_query=options.get("query"),
        )

        # Rebuild raw_tree from the sliced elements so the text reflects the
        # returned subset (avoids the previous 46k-token Chrome output).
        ui_lines: list[str] = []
        for e in sliced:
            ln = f"  [{e.ref}] {e.role}"
            if e.name:
                ln += f" {e.name!r}"
            if e.value:
                ln += f" = {e.value!r}"
            ui_lines.append(ln)
        # Keep the structured-roles context lines too if non-compact; under
        # compact we already dropped bulky fields, so we just want refs.
        if page_meta["n_returned"] and not bool(options.get("compact")):
            # Show expansion hint when truncated.
            if page_meta["tree_truncated"]:
                ui_lines = [
                    f"# Showing {page_meta['n_returned']} of "
                    f"{page_meta['n_total']} interactive elements "
                    f"(offset={int(options.get('offset') or 0)}, "
                    f"limit={options.get('limit')}). Re-run with different "
                    f"--offset/--limit to see more."
                ] + ui_lines

        routing_meta: dict[str, Any] = {
            "layer": self.name,
            "available": True,
            "n_refs": len(elements),  # total in this tree
            "n_returned": page_meta["n_returned"],
            "n_total": page_meta["n_total"],
            "n_interactive": page_meta["n_interactive"],
            "n_raw_lines": len(ui_lines),
            "tree_truncated": page_meta["tree_truncated"],
            "trusted": True,  # parity with desktop_ax's meta
        }
        if web_app_meta is not None:
            routing_meta["web_app"] = web_app_meta
            # Prepend the hint to raw_tree so it's the first thing the LLM sees.
            hint_lines = web_app_hint.splitlines() if web_app_hint else []
            ui_lines = hint_lines + ["", "# --- AX UI tree (browser chrome only) ---"] + ui_lines

        # Embedded web view (WKWebView/Electron): the Apple Events walk sees
        # only the shell; the DOM is opaque. Surface a structured hint with
        # url=None (most WKWebView apps expose no scriptable tab model) and a
        # raw_tree preface telling the LLM the concrete escalation path.
        if found_web_area:
            routing_meta["web_view"] = {
                "app": app,
                "url": None,
                "reason": (
                    "AXWebArea detected: the app embeds an opaque web view "
                    "(WKWebView/Electron) whose DOM is unreachable via Apple "
                    "Events. To act on page content, end this session and "
                    "restart in web context against the QCU Chromium daemon."
                ),
            }
            wv_preface = (
                f"# ⚠ 检测到内嵌 web view（AXWebArea）— DOM 对 Apple Events 不可见。\n"
                f"# 要操作页面内容：结束当前会话，用 web context 启动 QCU 自带的 Chromium：\n"
                f"#   qcu session end  →  qcu session start --context web  →  navigate <url>\n"
                f"# （QCU 无法自动获取该 web view 的 URL；需要你提供。）"
            )
            ui_lines = [wv_preface, ""] + ui_lines
        # Full browser (Chrome/Safari/Edge/Arc) detected via AppleScript tab
        # probing. Stock Chrome does NOT expose AXWebArea without
        # AXManualAccessibility, so the AXWebArea branch above never fires for
        # it — yet its DOM is just as opaque. Mirror the structured web_view
        # flag here too so the router (has_web_area AND the new has_web_app)
        # trips, and emit a stderr hint. Parity with desktop_ax.py.
        elif web_app_meta is not None:
            browser_name = web_app_meta.get("browser") or app
            first_tab_url = None
            tabs = web_app_meta.get("tabs") or []
            if tabs and isinstance(tabs[0], dict):
                first_tab_url = tabs[0].get("url")
            routing_meta["web_view"] = {
                "app": browser_name,
                "url": first_tab_url,
                "reason": (
                    f"Frontmost app is a browser ({browser_name}) detected via "
                    "AppleScript tab probing; its web DOM is opaque to Apple "
                    "Events. To act on page content, end this session and restart "
                    "in web context, navigating QCU's own Chromium to the same URL."
                ),
            }
            import sys as _sys
            print(
                f"[qcu] 前台是浏览器 {browser_name!r}：DOM 对 Apple Events 不透明，"
                f"建议 `qcu session end && qcu session start --context web` 后 "
                f"navigate 到同一 URL（如 {first_tab_url!r}）。",
                file=_sys.stderr,
            )

        return Observation(
            context="desktop",
            url_or_app=app,
            title=title,
            elements=sliced,
            routing_meta=routing_meta,
            raw_tree="\n".join(ui_lines) if ui_lines else None,
        )

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------

    def act(self, action: Action) -> LayerResult:
        # Positional System Events paths do not prove element/window identity.
        # Retain this adapter for diagnostic reads, but do not allow its legacy
        # action helpers to bypass the native ref and result contracts.
        return LayerResult(ok=False, layer=self.name, dispatch_state="not_sent",
            message="unsupported action type: Apple Events compatibility backend is read-only; observe desktop_ax and use its new refs",
            data={"reason": "backend_read_only", "supported_actions": [], "retry_safe": False})

    def _legacy_act(self, action: Action) -> LayerResult:
        """Historical implementation retained for migration review; not routed."""
        if not self.is_available(prompt=False):
            return LayerResult(
                ok=False,
                layer=self.name,
                message="osascript unavailable or Automation permission not granted",
            )

        atype = action.type
        params = action.params or {}

        # App-management actions are shared with desktop_ax; keep the same
        # params shape so callers don't care which layer they hit.
        if atype == "launch_app":
            return self._launch_app(params.get("app", ""))
        if atype == "activate_app":
            return self._activate_app(params.get("app", ""))
        if atype == "wait":
            ms = _wait_ms(params)
            time.sleep(ms / 1000.0)
            return LayerResult(ok=True, layer=self.name, message=f"waited {ms}ms")

        if atype in ("click", "double_click", "right_click", "hover"):
            return self._click(atype, params)
        if atype == "fill":
            return self._fill(params)
        if atype == "type":
            return self._type_text(params.get("text") or "")
        if atype == "press_key":
            return self._press_key(params.get("key", ""))
        if atype == "scroll":
            return self._scroll(params)
        if atype == "screenshot":
            # Delegate to desktop_ax's Quartz screenshot when available; this
            # layer never claims to do screenshots itself. The router should
            # not normally route screenshot here.
            try:
                from qcu.layers.runtime import get_layer

                ax = get_layer("desktop_ax")
                return ax._screenshot(params.get("path"))  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                return LayerResult(
                    ok=False, layer=self.name, message=f"screenshot delegation failed: {e}"
                )
        return LayerResult(
            ok=False, layer=self.name, message=f"unsupported action type: {atype}"
        )

    # ------------------------------------------------------------------
    # Action implementations
    # ------------------------------------------------------------------

    def _target_app_name(self) -> Optional[str]:
        """Pick the app whose tree to walk.

        Priority:
        1. ``self._last_app`` (set by a prior launch_app/activate_app and
           restored from session on __init__).
        2. The current frontmost application per System Events.
        """
        if self._last_app:
            return self._last_app
        try:
            res = subprocess.run(
                [
                    "osascript",
                    "-e",
                    'tell application "System Events" to name of first process whose frontmost is true',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                name = res.stdout.strip()
                # System Events itself reports itself as frontmost when no app
                # owns the menu bar — ignore that.
                if name and name.lower() != "system events":
                    self._last_app = name
                    return name
        except Exception:
            pass
        return None

    def _run_snippet(self, snippet: str, timeout: float = 10.0) -> tuple[bool, str, str]:
        """Run an inline osascript snippet with `tell process APP` wrapping.

        Returns (ok, stdout, stderr). The snippet body should NOT include its
        own `tell application "System Events"` block; we add one so callers
        stay terse and the process name stays consistent.
        """
        app = self._last_app or ""
        if not app:
            return False, "", "no target app set; call activate_app/launch_app or observe first"
        # We allow `keeTellExternally = true` here: some actions (keystroke,
        # set the clipboard) genuinely need to talk to System Events at the
        # top level, not inside a process block. The wrapper below puts the
        # snippet inside a process tell and re-exports common commands via
        # `my` — that's enough for click/fill/key.
        wrapped = (
            f'tell application "System Events"\n'
            f"  tell process {_as_quote(app)}\n"
            f"{snippet}\n"
            f"  end tell\n"
            f"end tell"
        )
        try:
            res = subprocess.run(
                ["osascript", "-e", wrapped],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, "", f"osascript timed out (>{timeout}s)"
        return res.returncode == 0, res.stdout, res.stderr

    def _click(self, atype: str, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        # Path 1: positional ref — most robust against name drift.
        if isinstance(ref, str):
            cached = self._last_refs.get(ref)
            if cached is None:
                return LayerResult(ok=False, layer=self.name, message=f"unknown ref {ref!r}")
            path = cached.get("path") or ""
            if not path:
                return LayerResult(
                    ok=False, layer=self.name, message=f"ref {ref!r} has no positional path"
                )
            chain = _path_to_chain(path)
            if not chain:
                return LayerResult(
                    ok=False, layer=self.name, message=f"ref {ref!r} path unresolvable"
                )
            # ``perform action "AXPress"`` is the most background-friendly
            # path: it dispatches directly to the element's action handler
            # without WindowServer activation. We verified on macOS 26 that
            # AXPressing a button in a *background* app keeps the current
            # frontmost untouched. ``click UI element N`` (System Events'
            # own command) is a fallback — it usually also doesn't activate,
            # but is slower and equivalent on most apps.
            if atype == "click":
                ok, _out, err = self._run_snippet(
                    f'perform action "AXPress" of {chain}'
                )
                if ok:
                    return LayerResult(
                        ok=True,
                        layer=self.name,
                        message=f"click ref={ref} (via AXPress, background-safe)",
                    )
                # Some apps expose AXOpen / AXConfirm / AXToggle instead of
                # AXPress (e.g. checkboxes). Fall through the action chain.
                ok, _out, err = self._run_snippet(
                    f'perform action "AXConfirm" of {chain}'
                )
                if ok:
                    return LayerResult(
                        ok=True,
                        layer=self.name,
                        message=f"click ref={ref} (via AXConfirm)",
                    )
                # Last System Events attempt — its `click` adds no value over
                # AXPress for activation concerns, but tolerates a few app-
                # specific AX providers that don't expose AXPress in SE's view.
                ok, _out, err = self._run_snippet(f"click {chain}")
                if ok:
                    return LayerResult(
                        ok=True, layer=self.name, message=f"click ref={ref} (via AppleScript click)"
                    )
                # Failure sometimes means the element is not literal-clickable
                # (menus, certain SWCell). Fall through to coordinate click.
            elif atype == "right_click":
                ok, _out, err = self._run_snippet(f'perform action "AXShowMenu" of {chain}')
                if ok:
                    return LayerResult(
                        ok=True, layer=self.name, message=f"right_click ref={ref} (via AXShowMenu)"
                    )
            elif atype == "hover":
                # AppleScript has no hover. We'll coordinate-move below; only
                # fall through intentionally.
                pass
            elif atype == "double_click":
                # Plain AXPress + AXPress with a tiny gap is the closest we
                # get to a native double-click via System Events.
                ok, _o, err = self._run_snippet(
                    f'perform action "AXPress" of {chain}\n'
                    f"delay 0.05\n"
                    f'perform action "AXPress" of {chain}'
                )
                if ok:
                    return LayerResult(
                        ok=True, layer=self.name, message=f"double_click ref={ref} (via AXPress x2)"
                    )

        # Path 2: coordinate click. Resolve ref → bounds, or take raw x,y.
        # NOTE on background-safety: when trigger.target_app != current
        # frontmost, CGEventPost lands in whichever window occupies (x, y)
        # at that moment — which may belong to the foreground app, not the
        # target. So coordinate clicking is only meaningful when the target
        # is currently frontmost. Callers who want drive-by clicks on a
        # background app have to use ref-based clicks (Path 1).
        cx, cy = self._resolve_xy(params)
        if cx is None or cy is None:
            return LayerResult(
                ok=False, layer=self.name, message="click needs ref with bounds, or x,y"
            )
        # Click-safety gate (Bug: 多显示器/遮挡下坐标点击落到飞书等更高栈序窗口).
        # Runs before BOTH the desktop_ax delegation and the SE `click at`
        # fallback so the wrong-window abort covers every backend. The gate
        # refuses input when Quartz ownership inspection is unavailable.
        from qcu.layers._click_safety import (
            assert_click_target, format_safety_failure,
        )

        app_window_bounds: Optional[dict[str, Any]] = None
        if self._last_app:
            try:
                from qcu.layers._vision import resolve_wid

                win = resolve_wid(self._last_app)
                if win is not None:
                    app_window_bounds = win.get("bounds")
            except Exception:
                app_window_bounds = None
        safety = assert_click_target(
            self._last_app, cx, cy, app_window_bounds=app_window_bounds,
        )
        if not safety["ok"]:
            return LayerResult(
                ok=False, layer=self.name,
                message=format_safety_failure(safety),
                data={"click_safety": safety},
            )
        # Coordinate-level mouse is delegated to desktop_ax's CGEvent path
        # (no Apple Events state machine for raw HID), with a clean fall-back
        # to System Events `click at {x, y}`.
        try:
            from qcu.layers.runtime import get_layer

            ax = get_layer("desktop_ax")
            if ax.is_available():  # type: ignore[attr-defined]
                return ax._click(atype, {"x": cx, "y": cy})  # type: ignore[attr-defined]
        except Exception:
            pass
        # SE `click at` is the last resort — it can be flaky for some apps.
        ok, _o, err = self._run_snippet(
            f"click at {{{cx}, {cy}}}",
            timeout=3.0,
        )
        if ok:
            return LayerResult(
                ok=True, layer=self.name, message=f"{atype} at ({cx},{cy}) (via System Events)"
            )
        return LayerResult(
            ok=False, layer=self.name, message=f"click at ({cx},{cy}) failed: {(err or '').strip()[:200]}"
        )

    def _fill(self, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        text = params.get("text", "")
        if not isinstance(ref, str):
            return LayerResult(ok=False, layer=self.name, message="fill needs params.ref")
        cached = self._last_refs.get(ref)
        if cached is None:
            return LayerResult(ok=False, layer=self.name, message=f"unknown ref {ref!r}")
        path = cached.get("path") or ""
        chain = _path_to_chain(path)
        if not chain:
            return LayerResult(ok=False, layer=self.name, message=f"ref {ref!r} path unresolvable")
        # Preferred: native set value — atomic, no focus steal, no clipboard.
        ok, _o, err = self._run_snippet(
            f"set value of {chain} to {_as_quote(text)}"
        )
        if ok:
            return LayerResult(
                ok=True,
                layer=self.name,
                message=f"fill ref={ref} with {len(str(text))} chars (via set value)",
            )
        # Fallback: click the field to focus, then type via paste.
        click_res = self._click("click", {"ref": ref})
        if not click_res.ok:
            return click_res
        return self._type_text(str(text), via_paste=True)

    def _type_text(self, text: str, *, via_paste: bool = True) -> LayerResult:
        """Type arbitrary text via Quartz ``CGEventKeyboardSetUnicodeString``.

        Why this exists instead of ``keystroke``: System Events refuses to
        synthesize HID events from osascript subprocess context
        ("osascript is not allowed to send keystrokes. (1002)"), and even
        when AX is trusted on our process the keystroke path is brittle on
        non-ASCII. Direct CGEvent routes Unicode through Quartz and works
        regardless of layout — verified for ASCII + CJK on macOS 26.

        Fallback ladder:
          1. ⌨ Quartz ``type_text`` (preferred; works for any Unicode)
          2. ⌘ Quartz paste (sets clipboard then Cmd+V via CGEvent — fast for
             long text and the only path that survives non-ASCII when Input
             Monitoring TCC partially denied)
          3. ⌨ osascript paste (last resort when Quartz import failed
             entirely; will fail on Input Monitoring denial)
        """
        if not text:
            return LayerResult(ok=True, layer=self.name, message="typed 0 chars (empty)")

        # Path 1: char-by-char via Quartz Unicode API.
        try:
            from qcu.layers._keyboard import type_text as quartz_type

            if quartz_type(text):
                return LayerResult(
                    ok=True,
                    layer=self.name,
                    message=f"typed {len(text)} chars (via Quartz CGEvent)",
                )
        except Exception:
            pass

        # Path 2: clipboard paste via Quartz CGEvent.
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString
            from qcu.layers._keyboard import press_key as quartz_press

            pb = NSPasteboard.generalPasteboard()
            saved = pb.stringForType_(NSPasteboardTypeString)
            change_count = pb.changeCount()
            pb.clearContents()
            pb.setString_forType_(text, NSPasteboardTypeString)
            ok_press = quartz_press("Cmd+V")
            import time as _t
            _t.sleep(0.05)
            # Restore clipboard if we were the last writer.
            try:
                if pb.changeCount() > change_count and saved is not None:
                    pb.clearContents()
                    pb.setString_forType_(saved, NSPasteboardTypeString)
            except Exception:
                pass
            if ok_press:
                return LayerResult(
                    ok=True,
                    layer=self.name,
                    message=f"typed {len(text)} chars (via Quartz Cmd+V paste)",
                )
        except Exception:
            pass

        # Path 3 (rare): osascript paste. Will likely fail with -1002 when
        # Input Monitoring is denied, but lowers the bar to a clean LayerResult.
        ok, _o, err = self._run_snippet(
            f"set the clipboard to {_as_quote(text)}\n"
            f'keystroke "v" using command down'
        )
        if ok:
            return LayerResult(
                ok=True, layer=self.name, message=f"typed {len(text)} chars (via osascript paste)"
            )
        return LayerResult(
            ok=False, layer=self.name, message=f"type failed (all 3 paths): {(err or '').strip()[:200]}"
        )

    def _press_key(self, key: str) -> LayerResult:
        """Press a key / key combo via Quartz ``CGEvent``.

        Examples: "Enter", "Tab", "Cmd+Q", "Cmd+Shift+3", "Escape".

        The previous AppleScript ``key code N`` path was rejected with
        "osascript is not allowed to send keystrokes. (1002)" because
        System Events in subprocess context can't dispatch HID. Direct
        ``CGEventCreateKeyboardEvent`` + ``CGEventPost`` is the right path;
        it needs only Input Monitoring TCC (not Accessibility, not
        Automation), and works for non-modifier keys without forcing the
        target app to the foreground.

        Returns ``ok=True`` once the event is dispatched. Note: Quartz
        CI events are delivered to whatever window owns the keyboard
        focus at the moment of dispatch, so the caller is responsible for
        confirming the action took effect (we surface this in the message).
        """
        if not key:
            return LayerResult(ok=False, layer=self.name, message="press_key needs params.key")

        try:
            from qcu.layers._keyboard import press_key as quartz_press, parse_key_combo

            ok = quartz_press(key)
            if ok:
                code, mods = parse_key_combo(key)
                # Build a friendly "(via Quartz, code=N modifiers=…)" suffix
                # so callers can audit the path. We don't claim "succeeded"
                # beyond "event was dispatched" — QCU can't verify receipt
                # without a follow-up observe().
                return LayerResult(
                    ok=True,
                    layer=self.name,
                    message=(
                        f"press_key {key!r} (via Quartz CGEvent; "
                        f"keycode={code}, mods=0x{mods:x}; "
                        f"target receives only if it is the foreground app)"
                    ),
                )
            # Parse failed: key spec didn't resolve to a known keycode.
            code, mods = parse_key_combo(key)
            if code is None:
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=(
                        f"press_key {key!r}: primary key not in the keycode table; "
                        f"add it to qcu.layers._keyboard.KEYCODES"
                    ),
                )
            return LayerResult(
                ok=False,
                layer=self.name,
                message=(
                    f"press_key {key!r} (keycode={code}, mods=0x{mods:x}) dispatch failed "
                    f"— likely missing Input Monitoring TCC for the QCU binary"
                ),
            )
        except Exception as e:  # noqa: BLE001
            return LayerResult(
                ok=False,
                layer=self.name,
                message=f"press_key {key!r}: {type(e).__name__}: {e}",
            )

    def _fallback_to_ax_press_key(self, key: str) -> Optional[LayerResult]:
        """Legacy fallback to desktop_ax._press_key.

        Kept for the rare case where desktop_ax has a newer keycode / a
        Quartz import we couldn't do here. Returns None on any import/path
        issue so the caller surfaces a clear failure to the LLM.
        """
        try:
            from qcu.layers.runtime import get_layer

            ax = get_layer("desktop_ax")
            if not ax.is_available():  # type: ignore[attr-defined]
                return None
            return ax._press_key(key)  # type: ignore[attr-defined]
        except Exception:
            return None

    def _scroll(self, params: dict[str, Any]) -> LayerResult:
        """Scroll via N repetitions of Page Down / Up or arrow keys.

        CGEvent scroll wheel living in desktop_ax is far more precise; this
        is what we can reach over Apple Events alone.
        """
        dy = int(params.get("dy", 0))
        dx = int(params.get("dx", 0))
        if dy == 0 and dx == 0:
            return LayerResult(ok=True, layer=self.name, message="scroll 0 (noop)")
        try:
            from qcu.layers.runtime import get_layer

            ax = get_layer("desktop_ax")
            if ax.is_available():  # type: ignore[attr-defined]
                return ax._scroll({"dx": dx, "dy": dy})  # type: ignore[attr-defined]
        except Exception:
            pass
        # Apple Events fallback: send N arrow keystrokes.
        def _send(key: str, count: int) -> LayerResult:
            for _ in range(max(0, min(count, 60))):
                self._press_key(key)
            return LayerResult(ok=True, layer=self.name, message=f"scrolled {key} x {count}")
        if dy > 0:
            return _send("Down", dy)
        if dy < 0:
            return _send("Up", -dy)
        if dx > 0:
            return _send("Right", dx)
        return _send("Left", -dx)

    def _launch_app(self, app: str) -> LayerResult:
        """Launch (or reuse) an app **without bringing it to the foreground**.

        Delegates to :mod:`qcu.layers._app_launch` so the background-launch
        logic doesn't drift between this layer and desktop_ax. See that
        module's docstring for the launch ladder and the ``focus_disturbed``
        flag semantics.
        """
        from qcu.layers._app_launch import launch_app_background

        dispatch = launch_app_background(app)
        ok, msg, focus_disturbed = dispatch
        state = getattr(dispatch, "dispatch_state", "sent" if ok else "not_sent")
        if not ok:
            return LayerResult(ok=False, layer=self.name, message=msg, dispatch_state=state,
                               data={"reason": "outcome_unknown"} if state == "unknown" else {})
        # Already-running apps register immediately; cold launches take ~0.4s.
        time.sleep(0.4)
        self._last_app = (app or "").strip()
        self._last_title = None
        try:
            from qcu.session import patch

            patch(target_app=app, context="desktop", layer=self.name, current_url=None)
        except Exception:
            pass
        marker = " (foreground — focus disturbed)" if focus_disturbed else " (background)"
        return LayerResult(ok=True, layer=self.name, message=msg + marker, dispatch_state=state)

    def _activate_app(self, app: str) -> LayerResult:
        """Bring an app to the foreground explicitly.

        Kept separate from ``launch_app`` so callers that *do* want focus-
        grabbing (presenting a slide, recording a tutorial) can ask for it.
        Default desktop automation should call ``launch_app`` which keeps
        the target in the background.
        """
        from qcu.layers._app_launch import activate_app

        dispatch = activate_app(app)
        ok, msg = dispatch
        state = getattr(dispatch, "dispatch_state", "sent" if ok else "not_sent")
        if ok:
            self._last_app = (app or "").strip()
        return LayerResult(ok=ok, layer=self.name, message=msg, dispatch_state=state,
                           data={"reason": "outcome_unknown"} if state == "unknown" else {})

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_xy(self, params: dict[str, Any]) -> tuple[Optional[float], Optional[float]]:
        if "x" in params and "y" in params:
            return float(params["x"]), float(params["y"])
        ref = params.get("ref")
        if isinstance(ref, str):
            cached = self._last_refs.get(ref) or {}
            cx = cached.get("cx")
            cy = cached.get("cy")
            if cx is not None and cy is not None:
                return float(cx), float(cy)
        return None, None

    def close(self) -> None:
        # Stateless: every call is a fresh osascript subprocess. Nothing to
        # close. We keep _last_refs so act() after observe() works.
        return None


# ---------------------------------------------------------------------------
# Module-level helpers (kept private).
# ---------------------------------------------------------------------------


def _as_quote(s: str) -> str:
    """Wrap a string as an AppleScript double-quoted literal."""
    return '"' + _as_escape(s) + '"'


def _serialize_refs(refs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten the in-memory ref cache to JSON-able dicts for the session."""
    out: list[dict[str, Any]] = []
    for ref, info in refs.items():
        b = info.get("bounds")
        out.append(
            {
                "ref": ref,
                "role": info.get("role"),
                "role_raw": info.get("role_raw"),
                "name": info.get("name"),
                "value": info.get("value"),
                "path": info.get("path"),
                "cx": info.get("cx"),
                "cy": info.get("cy"),
                "bounds": (
                    {"x": b.x, "y": b.y, "width": b.width, "height": b.height}
                    if isinstance(b, Rect)
                    else None
                ),
            }
        )
    return out
