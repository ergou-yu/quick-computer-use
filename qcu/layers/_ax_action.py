"""Cross-layer click fallback helper.

desktop_ax and desktop_appleevents store different representations of the
same UI element. desktop_ax keeps a Python-side ``AXUIElement`` and a numeric
``path`` like ``[0, 0, 1, 0]`` (root-relative children indices). When the
AXUIElement can't be re-resolved — typically because QCU's process lost
Accessibility trust between observe and act — we fall back to **AppleScript
``perform action "AXPress" of UI element N of …``** built from that numeric
path. This needs only the *Automation* TCC grant, not Accessibility, so it
keeps click working in the exact scenario ZCode reported
("Chrome ref 点击时报 'click needs ref or x,y' 但 ref 实际在缓存里").
"""

from __future__ import annotations

import subprocess
from typing import Any, Optional


def ax_path_list_to_applescript_chain(path: list[int], *, app: Optional[str] = None) -> Optional[str]:
    """Convert ``[0, 1, 0]`` into an AppleScript chain.

    Indexing rules are 1-based in AppleScript. The list semantics inherited
    from desktop_ax.walk: ``path[0]`` is the child index under the
    application root; ``path[1:]`` is children-of-children. So
    ``[0, 1, 0]`` maps to ``UI element 1 of UI element 2 of UI element 1 of
    process "X"``.

    Returns None when the path is empty / non-integers.
    """
    if not isinstance(path, list) or not path:
        return None
    try:
        indices = [int(i) + 1 for i in path]  # 0-based → 1-based
    except (TypeError, ValueError):
        return None
    # Build the chain from innermost outward.
    chain = ""
    for idx in reversed(indices):
        prefix = f"UI element {idx} of "
        chain = prefix + chain if chain else f"UI element {idx}"
    return chain


def perform_axpress_via_osascript(
    chain: str,
    *,
    app: str,
    action: str = "AXPress",
    timeout: float = 5.0,
) -> tuple[bool, str]:
    """Send ``perform action "<action>" of <chain>`` to ``process "app"``.

    Returns ``(ok, message)``. ok=True when osascript returned 0; message
    carries either a short status or the trimmed stderr for diagnosis.
    Does NOT activate the app — AppleScript ``perform action`` routes
    directly to the element's action handler, no WindowServer focus change.
    """
    if not app or not chain:
        return False, "perform_axpress: needs both app name and element chain"
    snippet = (
        f'tell application "System Events"\n'
        f'  try\n'
        f'    tell process "{_as_escape(app)}"\n'
        f"      perform action \"{action}\" of {chain}\n"
        f'    end tell\n'
        f'    return "ok"\n'
        f"  on error errMsg\n"
        f'    return "err:" & errMsg\n'
        f"  end try\n"
        f"end tell"
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", snippet],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"osascript timed out (>{timeout}s)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if res.returncode != 0:
        return False, (res.stderr or "").strip()[:200] or f"exit {res.returncode}"
    out = (res.stdout or "").strip()
    if out.startswith("err:"):
        return False, out[4:][:200]
    return True, out or "ok"


def perform_axpress_by_attributes(
    *,
    app: str,
    role: Optional[str] = None,
    subrole: Optional[str] = None,
    name: Optional[str] = None,
    action: str = "AXPress",
    timeout: float = 3.0,
) -> tuple[bool, str]:
    """Find an element by (role, subrole, name) inside ``process "app"`` and
    perform ``action`` on the first match.

    Why this exists: desktop_ax stores a numeric ``path`` from the AXUIElement
    tree, but AppleScript ``UI element N`` indexes use the System Events
    ``UI elements`` order — the two sequences often disagree (AXUIElement
    includes hidden nodes; SE doesn't), so a path-based chain can fail with
    "invalid index" on an element that genuinely exists. Falling back to a
    descriptor-based lookup matches the way users name things ("click the
    'New Tab' menu item"), works without re-walking the whole tree, and
    uses only the Automation TCC grant.

    Resolution ladder (fast → slow):
      1. ``menu_item`` role + non-empty name: walk only ``menu bar 1``'s
         menus (~150ms even for Chrome's 11 top-level menus × ~30 items).
         This is the case that previously timed out 6s on
         ``entire contents of window 1``.
      2. ``button``/``link``/etc. with name + subrole: scan ``window 1``'s
         ``entire contents`` with a hard cap of 2000 nodes or ``timeout``.
         Timeout default 3s — much shorter than the previous 6s because if
         we haven't matched by then we almost certainly won't.

    Returns ``(ok, message)``.
    """
    if not app:
        return False, "perform_axpress_by_attributes: missing app"

    # Step 1: menu_item role + supplied name → menu-bar scoped walk.
    # The role string can be either the normalized ("menu_item") or raw
    # ("AXMenuItem"). We compare case-insensitively.
    role_lc = (role or "").lower()
    is_menu = "menu" in role_lc
    if is_menu and name:
        snippet = (
            f'tell application "System Events"\n'
            f'  try\n'
            f'    tell process "{_as_escape(app)}"\n'
            f'      set hitMenu to missing value\n'
            f"      repeat with mi in menu bar items of menu bar 1\n"
            f'        try\n'
            f"          set ml to menu items of menu 1 of mi\n"
            f"        on error\n"
            f"          set ml to {{}}\n"
            f"        end try\n"
            f"        repeat with cand in ml\n"
            f'          try\n'
            f'            if (name of cand) is "{_as_escape(name)}" then\n'
            f"              -- CRITICAL: AXPress on a disabled item returns\n"
            f"              -- ok from System Events but the app does nothing.\n"
            f"              -- We must check enabled explicitly and bail.\n"
            f"              set en to true\n"
            f"              try\n"
            f"                set en to enabled of cand\n"
            f"              end try\n"
            f'              if en is false then return "disabled"\n'
            f"              perform action \"{action}\" of cand\n"
            f'              return "ok"\n'
            f"            end if\n"
            f"          end try\n"
            f"        end repeat\n"
            f"      end repeat\n"
            f'      return "none"\n'
            f"    end tell\n"
            f"  on error errMsg\n"
            f'    return "err:" & errMsg\n'
            f"  end try\n"
            f"end tell"
        )
        try:
            res = subprocess.run(
                ["osascript", "-e", snippet],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return False, f"menu-item lookup timed out (>{timeout}s)"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"
        out = (res.stdout or "").strip() if res.returncode == 0 else ""
        if out == "ok":
            return True, "ok (via menu-bar scan, enabled verified)"
        if out == "disabled":
            # Surface this — silently returning ok is exactly what misled
            # ZCode into thinking Cmd+S / Save worked.
            return False, f"menu item {name!r} is disabled (greyed out); AXPress would no-op"
        if out == "none":
            # Fall through to step 2 (whole-window scan) — some menu items
            # live in popup menus not under menu bar.
            pass
        elif out.startswith("err:"):
            return False, out[4:][:200]
        elif out:
            return False, out[:200]

    # Step 2: descriptor tuple (role + subrole + name). Build a where-clause
    # by attribute. System Events filters work over the role name AND the
    # AXSubrole string (it's literally exposed as a SE attribute). Name is
    # added when supplied.
    conds = []
    if role:
        conds.append(f"role of it is \"{role}\"")
    if subrole:
        conds.append(f"subrole of it is \"{subrole}\"")
    if name:
        conds.append(f"name of it is \"{_as_escape(name)}\"")
    where_clause = " and ".join(conds) if conds else "true"

    snippet = (
        f'tell application "System Events"\n'
        f'  try\n'
        f'    tell process "{_as_escape(app)}"\n'
        f"      set hits to {{}}\n"
        f"      set all to entire contents of window 1\n"
        f"      set scanned to 0\n"
        f"      repeat with el in all\n"
        f"        set scanned to scanned + 1\n"
        f"        if scanned > 2000 then exit repeat\n"
        f"        try\n"
        f"          if {where_clause} then\n"
        f"            set end of hits to el\n"
        f"            if (count of hits) > 4 then exit repeat\n"
        f"          end if\n"
        f"        end try\n"
        f"      end repeat\n"
        f"      set hitCount to count of hits\n"
        f"      if hitCount is 0 then return \"none\"\n"
        f"      if hitCount > 1 then return \"multiple:\"\n"
        f"      perform action \"{action}\" of (item 1 of hits)\n"
        f'      return "ok"\n'
        f"    end tell\n"
        f"  on error errMsg\n"
        f'    return "err:" & errMsg\n'
        f"  end try\n"
        f"end tell"
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", snippet],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"osascript timed out (>{timeout}s)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {e}"
    if res.returncode != 0:
        return False, (res.stderr or "").strip()[:200] or f"exit {res.returncode}"
    out = (res.stdout or "").strip()
    if out.startswith("err:"):
        return False, out[4:][:200]
    if out == "none":
        return False, "no element matched role/subrole/name"
    if out.startswith("multiple"):
        # Disambiguation hint surfaced to caller.
        return False, f"ambiguous: multiple elements match the descriptor ({out})"
    return True, out or "ok"


def _as_escape(s: str) -> str:
    if s is None:
        return ""
    out = []
    for ch in str(s):
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        else:
            out.append(ch)
    return "".join(out)
