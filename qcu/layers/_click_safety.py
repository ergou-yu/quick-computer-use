"""Click-safety gate for coordinate-based mouse actions.

The bug this module fixes: on multi-monitor setups (and even on a single
display), ``CGEventPost(kCGHIDEventTap, CGPoint(cx, cy))`` dispatches the
event to **whichever window owns that point in the global Quartz coordinate
space at dispatch time** — NOT to the app QCU thinks it is driving. AX and
CGEvent share the same coordinate system, so the issue is never a frame
mismatch; it is **z-order / ownership**. When a higher-stacking window
(飞书 / Clash / a notification banner) has moved over the cached AX point,
the click silently lands on the wrong app. This was reported in the field
as "坐标点击映射到了飞书窗口".

A second field report extended this: a **transparent / floating overlay**
(Mirroria, 悬浮球, a status item) sitting at a non-zero window level
(``kCGWindowLayer != 0``) can cover a normal (layer-0) Chrome window and
intercept every click, while macOS still reports Chrome as the frontmost
app. The original gate skipped every non-layer-0 window and was therefore
blind to overlays; the click passed the owner check (Chrome == Chrome) and
leaked through to the overlay. Reported as "QCU 报告 Chrome 在前台但实际
被覆盖". The fix: ``overlay_occluder`` walks the higher-stacked candidates
and aborts the click when a different-app, non-transparent overlay covers
the point.

The fix is a pure-function gate that runs *before* any CGEvent is posted:

1. ``window_at_point(x, y)`` walks ``CGWindowListCopyWindowInfo`` (which is
   returned **frontmost-first**, the same fact ``_vision.occlusion_check``
   relies on) and returns the first layer-0 window whose rect contains
   ``(x, y)``. That window is, by definition, the one that will receive a
   CGEvent posted at that point.
2. ``assert_click_target(app, x, y, app_window_bounds=None)`` compares the
   owning window's ``kCGWindowOwnerName`` against ``app`` (case-insensitive,
   ``.app`` suffix stripped — matching the convention used everywhere else
   in QCU), and optionally checks the point is still inside the target
   window's live bounds. Any mismatch → ``ok=False`` with the real owner
   named in the reason, so the LLM/user sees exactly what happened.

Policy (decided by the caller): on ``ok=False`` the click is **aborted**
with a clear error rather than posted at the wrong window. This matches the
approved plan ("Abort with clear error") and is consistent with
``--strict-background`` (a wrong-window click is exactly the foreground
drift that mode already refuses on).

The gate is **fail-open**: if Quartz / window-list introspection is
unavailable for any reason (import error, empty list, exception), it
returns ``ok=True`` with a ``reason`` — never introducing a new hard
dependency that could brick clicks on a stock machine.
"""

from __future__ import annotations

from typing import Any, Optional


def _owner_match(a: Optional[str], b: Optional[str]) -> bool:
    """Case-insensitive owner compare, stripping a trailing ``.app``.

    Mirrors the convention used in desktop_ax.observe / _resolve_ax_element:
    ``"Finder.app"`` and ``"finder"`` must compare equal so the safety gate
    doesn't false-positive on the trivial name-form differences macOS itself
    produces between NSWorkspace.localizedName and kCGWindowOwnerName.
    """
    if not a or not b:
        return False
    na = str(a).strip().lower().removesuffix(".app")
    nb = str(b).strip().lower().removesuffix(".app")
    return na == nb


def _point_in_bounds(x: float, y: float, b: dict[str, Any]) -> bool:
    """Inclusive point-in-rect for a ``kCGWindowBounds``-shaped dict."""
    try:
        bx = float(b["X"])
        by = float(b["Y"])
        bw = float(b["Width"])
        bh = float(b["Height"])
    except (KeyError, TypeError, ValueError):
        return False
    if bw <= 0 or bh <= 0:
        return False
    return (bx <= x <= bx + bw) and (by <= y <= by + bh)


def _window_list() -> Optional[list[dict[str, Any]]]:
    """Return the on-screen window list (frontmost-first), or None if the
    Quartz window-list API is unavailable on this host.

    Single source of truth for both ``window_at_point`` and the overlay
    occlusion check so they walk the same list in the same order.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception:
        return None
    try:
        return list(CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        ) or [])
    except Exception:
        return None


def _shape_window(w: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Normalize a raw CGWindowList entry into a flat dict, or None if it has
    no usable bounds. Pulled out so the occlusion check and window_at_point
    interpret the record identically."""
    try:
        bounds = dict(w.get("kCGWindowBounds") or {})
        if not bounds:
            return None
        # ``kCGWindowAlpha`` is optional in the window-list dict and is often
        # omitted entirely (meaning opaque = 1.0). Read it WITHOUT the
        # ``or 1.0`` truthiness trap: ``0.0 or 1.0`` evaluates to 1.0 because
        # 0.0 is falsy, which would silently turn a fully-transparent
        # (click-through) overlay into an opaque one.
        alpha_raw = w.get("kCGWindowAlpha", 1.0)
        try:
            alpha = float(alpha_raw)
        except (TypeError, ValueError):
            alpha = 1.0
        return {
            "owner": str(w.get("kCGWindowOwnerName") or ""),
            "wid": w.get("kCGWindowNumber"),
            "layer": int(w.get("kCGWindowLayer", 0) or 0),
            "alpha": alpha,
            "bounds": bounds,
            "title": str(w.get("kCGWindowTitle") or ""),
            "is_onscreen": bool(w.get("kCGWindowIsOnscreen", True)),
        }
    except Exception:
        return None


def windows_at_point(x: float, y: float) -> list[dict[str, Any]]:
    """Return EVERY on-screen window whose rect contains ``(x, y)``, in
    frontmost-first stacking order (lowest index = highest on screen).

    Unlike ``window_at_point`` this includes non-layer-0 windows (floating
    overlays, status items, modal sheets) — those are exactly the windows that
    can intercept a CGEvent posted at ``(x, y)`` while belonging to a different
    app than the intended target. Returns ``[]`` if Quartz is unavailable
    (callers treat that as fail-open).
    """
    wins = _window_list()
    if wins is None:
        return []
    out: list[dict[str, Any]] = []
    for w in wins:
        shaped = _shape_window(w)
        if shaped is None:
            continue
        if not _point_in_bounds(x, y, shaped["bounds"]):
            continue
        out.append(shaped)
    return out


def window_at_point(x: float, y: float) -> Optional[dict[str, Any]]:
    """Return the frontmost layer-0 window whose rect contains ``(x, y)``.

    Returns ``{owner, wid, bounds, title}`` or ``None``. The window list is
    ordered frontmost-first (lowest index = highest stacking), so the first
    layer-0 rect hit is the "normal" window a coordinate click is *intended*
    for. Overlay layers (``kCGWindowLayer != 0``) are excluded here so the
    menubar / SystemUIServer strip doesn't shadow real windows — the overlay
    case is handled separately by ``_overlay_occluder`` via ``windows_at_point``.
    """
    for w in windows_at_point(x, y):
        if w["layer"] != 0:
            continue
        return {
            "owner": w["owner"],
            "wid": w["wid"],
            "bounds": w["bounds"],
            "title": w["title"],
        }
    return None


def _is_occluding(w: dict[str, Any], expected_app: str) -> bool:
    """True iff window ``w`` would steal a click meant for ``expected_app``.

    A window occludes the target when:
      - it stacks at or above the target (caller passes only higher-stacked
        candidates), AND
      - it belongs to a DIFFERENT app than the intended target (same-app
        floating panels, like a TextEdit open panel, are not occluders), AND
      - it is not fully transparent (``kCGWindowAlpha`` ≈ 0 windows are
        click-through; alpha is absent → assume opaque = 1.0, the safe
        default).
    Layer is intentionally NOT filtered here — non-zero-layer windows
    (``NSFloatingWindowLevel`` / ``kCGOverlayWindowLevelKey`` etc.) are exactly
    the overlay windows (Mirroria, 悬浮球, 通知) that the old layer-0-only
    gate was blind to.
    """
    if _owner_match(w.get("owner"), expected_app):
        return False
    # Fully transparent (click-through) overlays don't intercept events.
    try:
        alpha = float(w.get("alpha", 1.0))
    except (TypeError, ValueError):
        alpha = 1.0
    if alpha <= 0.02:
        return False
    return True


def overlay_occluder(
    windows: list[dict[str, Any]], expected_app: str
) -> Optional[dict[str, Any]]:
    """Given the higher-stacked windows covering a point, return the first one
    that actually occludes ``expected_app`` (different owner, non-transparent),
    or None.

    Pure function — takes the candidate list from ``windows_at_point`` so it is
    trivially unit-testable without Quartz. The public ``assert_click_target``
    wires the real list in.
    """
    for w in windows:
        if _is_occluding(w, expected_app):
            return w
    return None


def assert_click_target(
    app: Optional[str],
    x: float,
    y: float,
    *,
    app_window_bounds: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Verify that a coordinate click at ``(x, y)`` would hit ``app``.

    Returns a verdict dict::

        {
          "ok": bool,            # False ⇒ caller MUST abort the click
          "reason": str,         # machine-readable code (see cases below)
          "owner": str | None,   # the actual frontmost window owner at (x,y)
          "expected_app": str,   # the app the caller intended to click in
          "x": float, "y": float,
        }

    Cases / reason codes:
      - ``"ok"``                       — owner matches app; safe to click.
      - ``"wrong_owner"``              — a different app owns the point; the
                                         real owner is named in ``owner`` so
                                         the LLM/user sees it landed on e.g.
                                         飞书.
      - ``"occluded_by_overlay"``      — owner matches app, BUT a higher-stacked
                                         overlay/floating window (different app,
                                         non-transparent) covers the point. The
                                         click would be stolen by the overlay
                                         (e.g. Mirroria / 悬浮球). Named in
                                         ``owner``; abort.
      - ``"point_not_in_any_window"``  — no layer-0 window contains the
                                         point (off-screen / menu strip /
                                         desktop). Abort.
      - ``"point_outside_target_window"`` — owner matches app, but the point
                                         is outside the caller-supplied
                                         target window bounds (window moved /
                                         resized since the ref was cached).
      - ``"safety_probe_unavailable"`` — Quartz/window-list introspection
                                         unavailable; **fail-open** ``ok=True``
                                         so this module never bricks clicks
                                         on a non-macOS or stripped-down host.
    """
    expected = (app or "").strip()
    base: dict[str, Any] = {
        "expected_app": expected or None,
        "x": float(x),
        "y": float(y),
    }

    # Snapshot the full candidate list ONCE — window_at_point and the overlay
    # check must walk the same list, and CGWindowListCopyWindowInfo is not
    # free. ``all_here`` is frontmost-first.
    all_here = windows_at_point(x, y)
    quartz_up = _quartz_available()

    # First pass: find the intended layer-0 target among the candidates.
    target_win: Optional[dict[str, Any]] = None
    for w in all_here:
        if w["layer"] != 0:
            continue
        target_win = w
        break

    if target_win is None:
        # Could be "no window here" OR "Quartz unavailable". If Quartz itself
        # is missing we fail-open (don't add a hard dep); if Quartz is present
        # and the list simply had no hit we hard-abort.
        if not quartz_up:
            return {**base, "ok": True, "reason": "safety_probe_unavailable", "owner": None}
        return {
            **base, "ok": False, "reason": "point_not_in_any_window", "owner": None,
        }

    owner = target_win["owner"]
    if not _owner_match(owner, expected):
        return {
            **base, "ok": False, "reason": "wrong_owner", "owner": owner,
            "owner_title": target_win.get("title"),
        }

    # Owner matches. Now check whether a higher-stacked window (overlay /
    # floating panel / status item belonging to a DIFFERENT app) covers the
    # point. CGEventPost dispatches by Quartz hit-test, which DOES honor
    # non-zero window levels — so an overlay above our target window steals
    # the click even though the layer-0 owner matches. This is the "Chrome
    # reported frontmost but Mirroria covered it" bug.
    #
    # Only candidates stacked strictly ABOVE the target window count: those at
    # a lower index in the frontmost-first list. Same-app overlays (e.g. the
    # target app's own modal sheet) are not occluders.
    target_idx = all_here.index(target_win)
    occluder = overlay_occluder(all_here[:target_idx], expected)
    if occluder is not None:
        return {
            **base, "ok": False, "reason": "occluded_by_overlay",
            "owner": occluder["owner"], "owner_title": occluder.get("title"),
            "occluder_layer": occluder.get("layer"),
        }

    # If the caller handed us the target app's *current* window bounds, also
    # confirm the point still sits inside it — the AX ref's cx/cy can be stale
    # if the window was dragged/resized between observe() and act(). Without
    # this, a stale coord inside the app's *other* window (or in dead space
    # the app no longer occupies) would pass the owner check but still miss
    # the intended control.
    if app_window_bounds is not None and not _point_in_bounds(x, y, app_window_bounds):
        return {
            **base, "ok": False, "reason": "point_outside_target_window",
            "owner": owner, "owner_title": target_win.get("title"),
        }
    return {**base, "ok": True, "reason": "ok", "owner": owner}


def _quartz_available() -> bool:
    """True iff the Quartz window-list API is importable on this host.

    Used only to distinguish "no window at point" (real, hard-abort) from
    "Quartz missing entirely" (environment, fail-open) so the safety gate
    degrades cleanly on non-macOS test machines.
    """
    try:
        from Quartz import (  # noqa: F401
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
        )
        return True
    except Exception:
        return False


def format_safety_failure(safety: dict[str, Any]) -> str:
    """Render a verdict dict into a clear, LLM-readable failure message.

    Names the real owner when the click would have hit a different app
    (the concrete signal that was missing in the 飞书 drift report —
    previously the click silently went to the wrong window with ok=True).
    """
    reason = safety.get("reason", "unknown")
    x = safety.get("x")
    y = safety.get("y")
    expected = safety.get("expected_app") or "<unknown>"
    owner = safety.get("owner")
    if reason == "wrong_owner":
        who = owner or "another app"
        extra = f" ({safety['owner_title']!r})" if safety.get("owner_title") else ""
        return (
            f"click at ({x},{y}) aborted: frontmost window at that point belongs "
            f"to {who!r}{extra}, not {expected!r}. Re-scope with --app/--window or "
            f"activate the target first."
        )
    if reason == "occluded_by_overlay":
        who = owner or "an overlay"
        extra = f" ({safety['owner_title']!r})" if safety.get("owner_title") else ""
        return (
            f"click at ({x},{y}) aborted: a floating/overlay window belonging to "
            f"{who!r}{extra} covers this point and would steal the click from "
            f"{expected!r}. Hide/quit the overlay or move the target window and "
            f"re-run `qcu observe`."
        )
    if reason == "point_not_in_any_window":
        return (
            f"click at ({x},{y}) aborted: no on-screen window contains this point "
            f"(off-screen / desktop / menu strip). The cached ref may be stale; "
            f"re-run `qcu observe`."
        )
    if reason == "point_outside_target_window":
        return (
            f"click at ({x},{y}) aborted: point is outside {expected!r}'s current "
            f"window bounds (window moved/resized since observe). Re-run "
            f"`qcu observe` to refresh refs."
        )
    return f"click at ({x},{y}) aborted: {reason}"
