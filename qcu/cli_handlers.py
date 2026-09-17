"""Handlers for each CLI sub-command.

Kept in a separate module so ``cli.py`` only does arg parsing.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

from qcu.common.types import Action, DEFAULT_CRITICAL_ACTIONS
from qcu.common import permissions
from qcu.data.collector import Telemetry, summary as telemetry_summary
from qcu.router.classifier import RoutingDecision, classify
from qcu.router.features import extract as extract_features
from qcu.session import clear, load, new, status_dict


# Commands that carry state worth keeping resident (browser handle, AX refs,
# target app). These are the ones we route through the daemon when one is up.
# Stateless/lightweight commands (schema, route, stats, --version) skip it.
_DAEMON_METHODS = {
    "observe", "act", "batch", "session_start", "session_status", "session_end",
    "browser_status", "browser_stop",
}


def _daemon_for_this_session() -> Optional[int]:
    """Return the daemon port recorded in the session, or None.

    We don't probe liveness here — the caller decides whether to (re)start.
    """
    s = load()
    if s is None:
        return None
    port = s.daemon_port
    if not port:
        return None
    return port


def _ensure_daemon_started() -> Optional[tuple[int, int]]:
    """Lazy-start the daemon if it isn't up, persist its pid/port, return them.

    Returns None if the daemon couldn't be started (caller falls back to local
    execution without aborting — the task still works, just slower). On success
    the session is patched so later commands in the same task reuse the daemon.
    """
    # If WE are the daemon, do nothing — routing back into ourselves would
    # recurse (we'd call observe -> it routes to daemon -> daemon calls observe
    # -> ...). The QCU_DAEMON_PROCESS env flag is set by the daemon's main().
    if os.environ.get("QCU_DAEMON_PROCESS") == "1":
        return None
    s = load()
    if s is None:
        # The first local operation creates the logical session. Launching a
        # daemon before that would lose its pid/port when patch() has no target.
        return None
    recorded_port = s.daemon_port if s is not None else None
    recorded_pid = s.daemon_pid if s is not None else None
    try:
        from qcu.daemon import launcher
        from qcu.daemon.server import health_check
        # Already alive? reuse without relaunch.
        if recorded_port and health_check(recorded_port, timeout=1.0):
            return recorded_pid or 0, recorded_port
        pid, port = launcher.ensure_daemon(recorded_port, recorded_pid)
    except Exception:
        return None
    try:
        from qcu.session import patch
        patch(daemon_pid=pid or None, daemon_port=port)
    except Exception:
        pass
    return pid, port


def _maybe_route_via_daemon(method: str, params: dict[str, Any]) -> Optional[int]:
    """If a daemon is up, send the request to it and replay its response.

    Returns the CLI exit code (so the caller returns immediately on success),
    or None to signal "no daemon / call failed — execute locally instead".
    """
    # Never route from inside the daemon itself — that would recurse.
    if os.environ.get("QCU_DAEMON_PROCESS") == "1":
        return None
    if method not in _DAEMON_METHODS:
        return None
    port = _daemon_for_this_session()
    if port is None:
        return None
    try:
        from qcu.daemon.server import call_daemon, DaemonError
    except Exception:
        return None
    try:
        resp = call_daemon(port, method, params, timeout=300.0)
    except DaemonError as exc:
        # After sending a request, a lost reply does not prove it failed.
        # Replaying a click or a batch locally could duplicate its side effect.
        if exc.dispatched:
            print(json.dumps({"ok": False, "error": str(exc),
                              "reason": "outcome_unknown", "retry_safe": False,
                              "dispatch_state": "unknown", "outcome": "unknown"}), file=sys.stderr)
            return 2
        return None
    if not resp.get("ok"):
        # Daemon-side handler error: surface it like a local error.
        print(
            json.dumps({"ok": False, "error": resp.get("error", "daemon error"),
                        "dispatch_state": "unknown", "outcome": "unknown",
                        "reason": "outcome_unknown", "retry_safe": False}),
            file=sys.stderr,
        )
        return 1
    result = resp.get("result")
    if resp.get("stderr"):
        print(resp["stderr"], end="", file=sys.stderr)
    if result is not None:
        # The handler's original stdout payload — print it unchanged so the
        # CLI's stdout contract is identical to local execution.
        if isinstance(result, str):
            print(result)
        else:
            print(json.dumps(result, ensure_ascii=False, indent=None if params.get("compact") else 2))
    return int(resp.get("exit_code", 0))


# ---------------------------------------------------------------------------
# Session commands
# ---------------------------------------------------------------------------


# Human-readable labels for permission keys, used in stderr hints.
_PERM_LABELS = {
    "accessibility": "Accessibility",
    "input_monitoring": "Input Monitoring",
    "screen_recording": "Screen Recording",
}


def _emit_missing_permission_hints(report: dict[str, dict[str, Any]]) -> None:
    """Print one stderr line per missing permission, with an `open` command.

    Goes to stderr so the stdout JSON contract stays clean. Called from
    `session_start` (desktop) and `doctor`.
    """
    for key, label in _PERM_LABELS.items():
        entry = report.get(key, {})
        if entry.get("granted") is False:
            url = entry.get("url", "")
            run_cmd = f"open '{url}'" if url else "(no pane URL)"
            print(
                f"Missing macOS permission — {label}: run {run_cmd}",
                file=sys.stderr,
            )
        elif entry.get("granted") is None and entry.get("reason"):
            # Couldn't probe; surface why so the user knows it's a blind spot.
            print(
                f"Could not probe {label}: {entry['reason']}",
                file=sys.stderr,
            )


def _emit_observation_blocker_hint(obs_payload: dict[str, Any]) -> None:
    """Emit a stderr hint when an observe/act result hit a permission or
    availability blocker.

    Why this exists: when desktop_ax/desktop_appleevents degrade (AX not
    trusted, Automation not granted, no frontmost app), they return a
    near-empty Observation with the cause in ``routing_meta.reason`` and a
    remediation preface in ``raw_tree``. But ``session_start``/``doctor``
    emit stderr hints while the ``observe``/``act`` paths did NOT — so an
    agent invoking observe directly saw an empty element list with no
    actionable guidance and gave up (the Kimi "無法讀取 UI" field report).

    This runs on the LOCAL (non-daemon) path only. The daemon path swallows
    the handler's stderr, but the ``raw_tree`` preface / ``message`` travels
    through the JSON payload unchanged, so the agent still sees the
    remediation there.
    """
    rm = obs_payload.get("routing_meta") or {}
    reason = str(rm.get("reason", "")).lower()
    # act() payloads carry the cause in `message` (a LayerResult), not routing_meta.
    message = str(obs_payload.get("message", "")).lower()
    combined = f"{reason} {message}"
    trusted = rm.get("trusted")
    available = rm.get("available")
    is_blocker = (
        trusted is False
        or available is False
        or any(kw in combined for kw in (
            "permission", "not granted", "unavailable", "not installed",
        ))
    )
    if not is_blocker:
        return
    if "accessibility" in combined or trusted is False:
        print(
            "[qcu] ⚠ 無法讀取/操作 UI:輔助使用(Accessibility)權限問題。"
            "執行 `qcu doctor` 確認,授權後 `qcu session end && "
            "qcu session start --context desktop` 重啟。詳見 raw_tree / message。",
            file=sys.stderr,
        )
    elif available is False or "unavailable" in combined or "not installed" in combined:
        print(
            "[qcu] ⚠ 無法讀取/操作 UI:層不可用(依賴缺失或 Automation 權限)。"
            "執行 `qcu doctor` 確認。詳見 raw_tree / message。",
            file=sys.stderr,
        )
    else:
        cause = rm.get("reason") or obs_payload.get("message") or "unknown"
        print(
            f"[qcu] ⚠ 操作受阻:{cause}。"
            "詳見 raw_tree / message 裡的補救步驟。",
            file=sys.stderr,
        )


def session_start(context: str, headless: Optional[bool] = None,
                  strict_background: bool = False,
                  no_screenshot_fallback: bool = False) -> int:
    # Resolve headed/headless ONCE here and persist it onto the session so the
    # browser daemon (started lazily later) reads `session.headless` instead of
    # the transient QCU_HEADLESS env var of whatever process happens to attach
    # first. Previously `QCU_HEADLESS=0 qcu session start` was lost because the
    # browser only launched on the NEXT command, which had no such env var.
    if headless is None:
        headless = os.environ.get("QCU_HEADLESS", "1") != "0"
    # NOTE: we deliberately DON'T _ensure_daemon_started() here. session_start
    # may call new()/clear() which would clobber the daemon fields the ensure
    # just persisted. The daemon is lazy-started by observe/act instead (the
    # commands that actually need it), after any session bookkeeping is done.
    rc = _maybe_route_via_daemon("session_start", {"context": context, "headless": headless,
                                                    "strict_background": strict_background,
                                                    "no_screenshot_fallback": no_screenshot_fallback})
    if rc is not None:
        return rc

    s = load()
    # If a session already exists, resume it — UNLESS the user passed an
    # explicit --context that conflicts with the existing one. Previously this
    # silently resumed and ignored --context, so `session start --context
    # desktop` after a web session was a no-op and you stayed on the browser
    # layer. An explicit context now wins over the stale session.
    if s is not None:
        if context != "auto" and s.context != context:
            # Conflicting explicit context: drop the old session so the new one
            # can be created with the right layer/context below. Don't kill the
            # browser daemon here — `session end` is the explicit shutdown path;
            # switching contexts only swaps the recorded layer.
            clear()
            s = None
        else:
            # If the requested headed/headless setting conflicts with the
            # persisted one, the live browser cannot switch its mode in place
            # (Chromium needs a relaunch). Record the conflict so the next
            # observe/browser-start triggers a clean restart, and warn the
            # caller rather than silently staying in the old mode.
            conflict = s.headless != headless
            if conflict:
                from qcu.session import patch as _patch

                _patch(headless=headless, browser_status="stopped")
                try:
                    from qcu.layers.runtime import close_active

                    close_active()
                except Exception:
                    pass
            print(
                json.dumps(
                    {
                        "ok": True,
                        "resumed": True,
                        "session": status_dict(),
                        "headless_changed": conflict,
                    },
                    ensure_ascii=False,
                )
            )
            return 0

    # Pick a default layer for the context. Real layer instances are
    # constructed lazily in observe()/act() so this just records intent.
    if context == "auto":
        # Web first; user can re-start with --context desktop.
        layer = "web_a11y"
        resolved = "web"
    elif context == "web":
        layer = "web_a11y"
        resolved = "web"
    elif context == "desktop":
        from qcu.platforms import desktop_backend_name
        layer = desktop_backend_name()
        resolved = "desktop"
    else:
        print(
            json.dumps({"ok": False, "error": f"unknown context: {context}"}),
            file=sys.stderr,
        )
        return 1

    # Desktop permission preflight. Probes all three TCC grants up front and
    # surfaces the macOS consent dialogs for any missing one, so the rest of
    # the task (observe/act/screenshot/press_key) isn't interrupted by a
    # permission prompt mid-flight. Non-blocking: a missing permission does
    # NOT abort session creation — the report is attached to the response so
    # the caller can warn the user. Web sessions skip this entirely.
    perm_report: dict[str, Any] | None = None
    if resolved == "desktop" and sys.platform == "darwin":
        try:
            perm_report = permissions.probe_all()
            permissions.trigger_prompts_for_missing(perm_report)
            _emit_missing_permission_hints(perm_report)
        except Exception as e:  # noqa: BLE001 — never wedge session start
            print(
                json.dumps(
                    {"ok": False, "warning": f"permission preflight failed: {e}"}
                ),
                file=sys.stderr,
            )

    new(context=resolved, layer=layer, headless=headless)
    # Persist session-level hard switches into session.extras so the layers
    # (running inside the resident daemon) can read them without a schema bump.
    # extras is the documented forward-compat extension point. patch() replaces
    # the whole dict, so we read-merge-write to preserve other keys (e.g.
    # ref_generation that web_a11y uses). For strict-background we also capture
    # the frontmost app at start time so the layer can detect drift mid-task.
    if strict_background or no_screenshot_fallback:
        expected_front: Optional[str] = None
        if strict_background:
            try:
                from AppKit import NSWorkspace  # type: ignore
                fm = NSWorkspace.sharedWorkspace().frontmostApplication()
                expected_front = (fm.localizedName() or "") if fm is not None else None
            except Exception:
                expected_front = None
        try:
            from qcu.session import load as _load, patch as _patch
            cur = _load()
            merged: dict[str, Any] = dict(getattr(cur, "extras", None) or {})
            if strict_background:
                merged["strict_background"] = True
                merged["expected_foreground"] = expected_front
            if no_screenshot_fallback:
                merged["no_screenshot_fallback"] = True
            _patch(extras=merged)
        except Exception:
            pass
    payload: dict[str, Any] = {
        "ok": True,
        "resumed": False,
        "session": status_dict(),
    }
    if resolved == "desktop":
        from qcu.layers.runtime import capability_report
        payload["capabilities"] = capability_report()
    if perm_report is not None:
        payload["permissions"] = perm_report
    print(json.dumps(payload, ensure_ascii=False))
    return 0


def session_status() -> int:
    print(json.dumps(status_dict(), ensure_ascii=False, indent=2))
    return 0


def session_end(purge_profile: bool = False) -> int:
    # 1. Drop this process's CDP connection (does NOT kill the daemon).
    try:
        from qcu.layers.runtime import close_active

        close_active()
    except Exception as e:  # noqa: BLE001
        print(
            json.dumps({"ok": False, "warning": f"close failed: {e}"}),
            file=sys.stderr,
        )

    # 2. Actually stop the detached daemon by pid. close_active() only
    # disconnected; the browser would otherwise keep running headless forever.
    stopped = None
    s = load()
    if s is not None and s.daemon_pid and s.daemon_pid != os.getpid():
        from qcu.layers.browser_daemon import kill_pid
        kill_pid(s.daemon_pid)
    if s is not None and s.browser_pid:
        try:
            from qcu.layers.browser_daemon import kill_pid

            kill_pid(s.browser_pid)
            stopped = s.browser_pid
        except Exception as e:  # noqa: BLE001
            print(
                json.dumps({"ok": False, "warning": f"daemon kill failed: {e}"}),
                file=sys.stderr,
            )

    # 2b. Optional QCU-managed profile purge. Only delete a profile that QCU
    # itself created (profile_owned) and only after the browser is stopped, so
    # we never recursively remove a user's own QCU_USER_DATA_DIR or follow
    # symlinks outside the QCU home.
    purged: Optional[str] = None
    purge_skipped: Optional[str] = None
    if purge_profile and s is not None and s.user_data_dir:
        try:
            from qcu.layers import browser_daemon as bd

            ok_p, why = bd.purge_profile(s.user_data_dir, owned=getattr(s, "profile_owned", False))
            if ok_p:
                purged = s.user_data_dir
            else:
                purge_skipped = why
        except Exception as e:  # noqa: BLE001
            purge_skipped = f"{type(e).__name__}: {e}"

    clear()
    payload: dict[str, Any] = {"ok": True, "active": False, "daemon_stopped_pid": stopped}
    if purged is not None:
        payload["purged_profile"] = purged
    if purge_skipped is not None:
        payload["purge_skipped"] = purge_skipped
    print(json.dumps(payload, ensure_ascii=False))
    return 0


# ---------------------------------------------------------------------------
# Observe
# ---------------------------------------------------------------------------


def observe(
    layer: Optional[str],
    max_depth: int,
    *,
    full_text: bool = False,
    text_limit: Optional[int] = None,
    tail: bool = False,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
    role: Optional[str] = None,
    name: Optional[str] = None,
    app: Optional[str] = None,
    pid: Optional[int] = None,
    window: Optional[str] = None,
    wait_until: str = "domcontentloaded",
    wait_for: Optional[str] = None,
    timeout_ms: int = 3000,
) -> int:
    # Create the logical session BEFORE daemon startup. Native refs must be
    # observed inside the persistent backend even on the first implicit start.
    s = load()
    desktop_scope = app is not None or pid is not None or window is not None
    if s is None:
        if desktop_scope or (layer and layer.startswith("desktop_")):
            from qcu.platforms import desktop_backend_name
            s = new(context="desktop", layer=layer or desktop_backend_name())
        else:
            s = new(context="web", layer="web_a11y")
    chosen_layer = layer or s.layer
    incompatible = ((s.context == "web" and (desktop_scope or chosen_layer.startswith("desktop_")))
                    or (s.context == "desktop" and chosen_layer in {"web_a11y", "webmcp"}))
    if incompatible:
        print(json.dumps({"ok": False, "error": "Observation target conflicts with the active session; start the intended context explicitly",
                          "reason": "target_mismatch", "context": s.context, "layer": chosen_layer}, ensure_ascii=False))
        return 2

    _ensure_daemon_started()
    opts = {
        "layer": layer,
        "max_depth": max_depth,
        "full_text": full_text,
        "text_limit": text_limit,
        "tail": tail,
        "limit": limit,
        "offset": offset,
        "compact": compact,
        "role": role,
        "name": name,
        "app": app,
        "pid": pid,
        "window": window,
        "wait_until": wait_until,
        "wait_for": wait_for,
        "timeout_ms": timeout_ms,
    }
    rc = _maybe_route_via_daemon("observe", opts)
    if rc is not None:
        return rc

    from qcu.layers.runtime import get_layer

    L = get_layer(chosen_layer)
    t0 = time.perf_counter()
    obs = L.observe(
        max_depth=max_depth,
        full_text=full_text,
        text_limit=text_limit if text_limit is not None else 80,
        tail=tail,
        limit=limit,
        offset=offset,
        compact=compact,
        roles=role,
        query=name,
        app=app,
        pid=pid,
        window=window,
        wait_until=wait_until,
        wait_for=wait_for,
        timeout_ms=timeout_ms,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000.0

    # A failed/empty observation is evidence about this target. Automatically
    # changing layers could discard its window scope or launch another browser.
    feats = extract_features(obs)
    decision = classify(feats, last_action=None)

    Telemetry.record(
        context=obs.context,
        features=feats,
        decision=decision,
        action=None,
        outcome=None,
        latency_ms=elapsed_ms,
        actual_layer=chosen_layer,
    )

    payload = obs.to_dict()
    payload["elapsed_ms"] = round(elapsed_ms, 2)
    payload["router_decision"] = decision.to_dict(compact=compact)
    payload["layer_used"] = chosen_layer
    if compact:
        # Serialize less, but retain false/zero states and routing diagnostics.
        payload["elements"] = [
            {k: v for k, v in e.items() if v is not None and v != {} and v != []
             and k != "backend_id"}
            for e in payload["elements"]
        ]
        payload = {k: v for k, v in payload.items() if k == "elements" or (v is not None and v != [])}
    # Surface a stderr hint when the observation hit a permission/availability
    # blocker — otherwise the agent sees an empty element list and gives up
    # (the Kimi field report). raw_tree already carries the preface; this is
    # the stderr companion for the local (non-daemon) path.
    _emit_observation_blocker_hint(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2))
    return 2 if obs.routing_meta.get("error") else 0


# ---------------------------------------------------------------------------
# Find / Inspect — operate on the persisted last real observation
# ---------------------------------------------------------------------------


def _last_observation_elements() -> list[dict[str, Any]]:
    """Return the persisted last observation's elements + cached ref metadata.

    ``find`` / ``inspect`` reuse the most recent real observation instead of
    forcing a fresh observe, so the LLM can quickly ask "where is 'More' in
    what I just saw?" without re-snapshotting the page.
    """
    s = load()
    if s is None:
        return []
    elts: list[dict[str, Any]] = []
    # Cached refs carry ref/role/name/bounds — enough for routing and for the
    # LLM to identify an element without re-scanning the whole tree.
    for r in s.last_refs or []:
        elts.append(
            {
                "ref": r.get("ref"),
                "role": r.get("role"),
                "name": r.get("name"),
                "bounds": r.get("bounds"),
            }
        )
    # Prefer the structured elements saved on last_observation when present.
    lo = getattr(s, "last_observation", None) or {}
    saved = lo.get("elements") or []
    if saved and not elts:
        elts = saved
    return elts


def find(
    role: Optional[str] = None,
    name: Optional[str] = None,
    limit: Optional[int] = None,
    offset: int = 0,
    compact: bool = False,
) -> int:
    s = load()
    if s is None:
        print(json.dumps({"ok": False, "error": "no active session; run `qcu session start`"}), file=sys.stderr)
        return 1
    elts = _last_observation_elements()
    needle = name.casefold() if name else None
    role_set = {p.strip() for p in role.split(",")} if role else None
    matches = [
        e for e in elts
        if (not role_set or e.get("role") in role_set)
        and (not needle or needle in (e.get("name") or "").casefold())
    ]
    start = max(0, offset)
    paged = matches[start:] if limit is None else matches[start:start + max(0, limit)]
    payload: dict[str, Any] = {
        "ok": True,
        "n_matches": len(matches),
        "n_returned": len(paged),
        "elements": [
            {k: v for k, v in e.items() if not (compact and v in (None, ""))} for e in paged
        ],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2))
    return 0


def inspect(ref: str, compact: bool = False) -> int:
    s = load()
    if s is None:
        print(json.dumps({"ok": False, "error": "no active session"}), file=sys.stderr)
        return 1
    for r in s.last_refs or []:
        if r.get("ref") == ref:
            payload = {"ok": True, "element": r}
            print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2))
            return 0
    lo = getattr(s, "last_observation", None) or {}
    for e in lo.get("elements") or []:
        if isinstance(e, dict) and e.get("ref") == ref:
            payload = {"ok": True, "element": e}
            print(json.dumps(payload, ensure_ascii=False, indent=None if compact else 2))
            return 0
    print(json.dumps({"ok": False, "error": f"unknown ref {ref!r}; re-run `qcu observe`"}), file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Act
# ---------------------------------------------------------------------------


def act(action_json: str, layer: Optional[str]) -> int:
    # Route through the daemon for the same warm-state / focus-stability reasons
    # as observe(). Critical for desktop: the daemon holds _target_app + live
    # ax_element refs across this and the prior observe, so act-by-ref resolves
    # instantly and the right app stays the AX target.
    _ensure_daemon_started()
    rc = _maybe_route_via_daemon("act", {"action": action_json, "layer": layer})
    if rc is not None:
        return rc

    from qcu.layers.runtime import get_layer

    try:
        action = Action.from_json(action_json)
    except Exception as e:
        print(
            json.dumps({"ok": False, "error": f"bad action JSON: {e}"}),
            file=sys.stderr,
        )
        return 1

    s = load()
    if s is None:
        s = new(context="web", layer="web_a11y")

    # Reconstruct the REAL last observation so routing/telemetry reflect the
    # page the agent was looking at when it decided to act. The previous code
    # fed the router an empty placeholder (url_or_app=None, n_elements=0,
    # roles={}) even right after an observe of 228 elements — corrupting the
    # canvas/critical/webmcp routing rules and the training telemetry.
    from qcu.common.types import Observation

    last_obs: Optional[Observation] = None
    if getattr(s, "last_observation", None):
        try:
            last_obs = Observation.from_dict(s.last_observation)  # type: ignore[arg-type]
        except Exception:
            last_obs = None
    if last_obs is None:
        last_obs = Observation(context=s.context, url_or_app=getattr(s, "current_url", None))
        feats = extract_features(last_obs)
        feats["observation_missing"] = True
    else:
        # Rebuild elements from the cached refs so role/url/n_interactive are
        # populated even though last_observation stores only a compact dict.
        try:
            cached_elements = []
            for r in s.last_refs or []:
                cached_elements.append(
                    {
                        "ref": r.get("ref"),
                        "role": r.get("role"),
                        "name": r.get("name"),
                    }
                )
            merged = last_obs.to_dict()
            if cached_elements and not merged.get("elements"):
                merged["elements"] = cached_elements
                last_obs = Observation.from_dict(merged)
        except Exception:
            pass
        feats = extract_features(last_obs)
    feats["action_type"] = action.type
    feats["action_in_critical_list"] = action.type in DEFAULT_CRITICAL_ACTIONS
    feats["needs_visual_confirm"] = feats["action_in_critical_list"]
    feats["action_params"] = {
        k: (str(v) if not isinstance(v, (str, int, float, bool, type(None))) else v)
        for k, v in (action.params or {}).items()
    }

    # The feature extractor can't tell whether a ref is still present without
    # a fresh observation. Use the session's cached refs (and the web layer's
    # in-memory ref cache) to set `target_in_a11y` truthfully. Without this,
    # every web action that carries a ref would fall through to
    # screenshot_fallback even though the a11y layer can still act on it.
    ref = action.params.get("ref") if isinstance(action.params, dict) else None
    if isinstance(ref, str):
        cached_refs = {r.get("ref") for r in (s.last_refs or [])}
        in_cache = ref in cached_refs
        # Also ask the live layer's cache if the layer exposes one.
        if not in_cache and s.context == "web":
            try:
                from qcu.layers.runtime import get_layer as _gl

                _wl = _gl("web_a11y")
                in_cache = ref in getattr(_wl, "_last_refs", {})  # type: ignore[attr-defined]
            except Exception:
                pass
        feats["target_in_a11y"] = in_cache

    decision = classify(feats, last_action=action)

    # Pick the layer: an explicit --layer override wins; otherwise the
    # router's decision (canvas/critical/webmcp/desktop-no-ax rules) decides;
    # only if both are absent do we fall back to the session's default layer.
    # Previously this ignored `decision.layer` entirely, which meant every
    # priority-95/100 "hard override" rule (e.g. force a screenshot before a
    # payment) was silently skipped.
    chosen_layer = layer or decision.layer or s.layer

    # A ref identifies a control on the observed backend. Routing must never
    # reinterpret it as a coordinate or move it to another control surface.
    ref_keys = [key for key in ("ref", "ref_from", "ref_to") if key in action.params]
    observed_layer = last_obs.routing_meta.get("layer") if last_obs else None
    if ref_keys:
        known = {entry.get("ref") for entry in (s.last_refs or [])}
        invalid = any(action.params[key] not in known for key in ref_keys)
        mismatch = bool(layer and observed_layer and layer != observed_layer)
        if invalid or mismatch:
            from qcu.common.types import LayerResult
            failure = LayerResult(False, layer or s.layer, "reference target changed; observe again",
                                  data={"reason": "stale_ref" if invalid else "target_mismatch"})
            print(failure.to_json())
            return 2
        chosen_layer = observed_layer or s.layer

    if (s.context == "desktop" and chosen_layer in {"web_a11y", "webmcp"}) or (
        s.context == "web" and chosen_layer.startswith("desktop_")):
        from qcu.common.types import LayerResult
        print(LayerResult(False, chosen_layer, "layer conflicts with session target",
                          data={"reason": "target_mismatch"}).to_json())
        return 2

    # Session-level hard switch: --no-screenshot-fallback. When set, refuse any
    # action the router would route to screenshot_fallback rather than silently
    # degrading to vision. This is a TRUE opt-out (reject, not downgrade): the
    # caller learns the a11y/AX layer couldn't reach the target and can decide
    # whether to retry differently. Only enforced when the router itself chose
    # screenshot_fallback — an explicit --layer screenshot_fallback override is
    # the user deliberately asking for it, so we honor that.
    if chosen_layer == "screenshot_fallback" and not layer:
        if (getattr(s, "extras", None) or {}).get("no_screenshot_fallback"):
            msg = ("screenshot_fallback disabled by session flag "
                   "(--no-screenshot-fallback); a11y/AX cannot reach this target")
            print(json.dumps({"ok": False, "layer": "screenshot_fallback",
                              "message": msg, "router_decision": decision.to_dict(),
                              "layer_used": "screenshot_fallback",
                              "disabled_by": "no_screenshot_fallback"}))
            Telemetry.record(
                context=s.context, features=feats, decision=decision,
                action=action.to_dict(), outcome="fail",
                latency_ms=0.0, requested_layer=layer, actual_layer=None,
            )
            return 2

    t0 = time.perf_counter()
    try:
        L = get_layer(chosen_layer)
    except Exception as e:
        from qcu.common.types import LayerResult
        print(LayerResult(False, chosen_layer, str(e), data={"reason": "backend_unavailable"}).to_json())
        return 2
    try:
        result = L.act(action)
    except Exception as e:  # noqa: BLE001
        print(
            json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}",
                        "dispatch_state": "unknown", "outcome": "unknown",
                        "reason": "outcome_unknown", "retry_safe": False}),
            file=sys.stderr,
        )
        Telemetry.record(
            context=s.context,
            features=feats,
            decision=decision,
            action=action.to_dict(),
            outcome="error",
            latency_ms=(time.perf_counter() - t0) * 1000.0,
            requested_layer=layer,
            actual_layer=None,
        )
        return 1
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    Telemetry.record(
        context=s.context,
        features=feats,
        decision=decision,
        action=action.to_dict(),
        outcome="ok" if result.ok else "fail",
        latency_ms=elapsed_ms,
        requested_layer=layer,
        actual_layer=chosen_layer,
        verification=result.data.get("verification") if hasattr(result, "data") else None,
    )

    payload = result.to_dict()
    payload["elapsed_ms"] = round(elapsed_ms, 2)
    payload["router_decision"] = decision.to_dict()
    payload["layer_used"] = chosen_layer
    # Surface a stderr hint when the act hit a permission/availability blocker
    # (e.g. AX not trusted). Same rationale as observe — the agent otherwise
    # sees ok=False with a terse message and no clear remediation path.
    _emit_observation_blocker_hint(payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if result.ok else 2


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def batch(actions_json: str, layer: Optional[str] = None, *,
          observe_after: bool = False, compact: bool = False) -> int:
    from qcu.batch import parse_actions, execute
    try:
        actions = parse_actions(actions_json)
    except (ValueError, TypeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc), "executed": 0}), file=sys.stderr)
        return 1
    _ensure_daemon_started()
    rc = _maybe_route_via_daemon("batch", {"actions": actions_json, "layer": layer,
                                         "observe_after": observe_after, "compact": compact})
    if rc is not None:
        return rc
    return execute(actions, layer=layer, observe_after=observe_after, compact=compact)


def route(features_arg: str) -> int:
    if features_arg == "-":
        feats_raw = sys.stdin.read()
    else:
        feats_raw = Path(features_arg).read_text()
    feats = json.loads(feats_raw) if feats_raw.strip() else {}
    decision = classify(feats, last_action=None)
    print(json.dumps(decision.to_dict(), ensure_ascii=False, indent=2))
    return 0


def stats(since: str, path: Optional[str]) -> int:
    summary = telemetry_summary(since=since, path=path)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


def schema(what: str) -> int:
    from qcu.cli_schema import SCHEMAS

    if what not in SCHEMAS:
        print(json.dumps({"ok": False, "error": f"unknown schema: {what}"}))
        return 1
    print(json.dumps(SCHEMAS[what], ensure_ascii=False, indent=2))
    return 0


def _doctor_install_info() -> dict[str, Any]:
    """Collect skill/import/executable paths, version, and git commit.

    This is what surfaces the "Codex reads the old skill copy while the
    editable install runs the new source" drift that originally hid the
    resident daemon and go_back from one of the agents. Every path is shown so
    a human can tell at a glance whether `qcu` actually runs from the source
    tree they're editing.
    """
    import importlib.util
    import shutil
    import subprocess
    import sys
    from pathlib import Path as _P

    import qcu as _qcu_pkg

    info: dict[str, Any] = {}
    # Where the `qcu` python package actually resolves from.
    info["import_path"] = getattr(_qcu_pkg, "__file__", None)
    try:
        info["package_version"] = _qcu_pkg.__version__
    except Exception:
        info["package_version"] = None
    # Editable-install metadata if available.
    try:
        from importlib.metadata import PackageNotFoundError, distribution

        dist = distribution("qcu")
        info["distribution_path"] = str(dist.locate_file(""))  # type: ignore[arg-type]
        info["installer"] = dist.read_text("INSTALLER") or None
    except Exception as e:  # noqa: BLE001
        info["distribution_error"] = f"{type(e).__name__}: {e}"
    # The `qcu` console-script entrypoint, if present on PATH.
    try:
        info["executable_path"] = shutil.which("qcu") or None
        info["python_executable"] = sys.executable
    except Exception:
        pass
    # The skill dossier (SKILL.md) location the agent would actually read.
    try:
        from pathlib import Path as _P

        pkg_dir = _P(_qcu_pkg.__file__).resolve().parent.parent  # repo root guess
        skill_md = pkg_dir / "SKILL.md"
        info["skill_md_path"] = str(skill_md) if skill_md.exists() else None
    except Exception:
        info["skill_md_path"] = None
    # Git commit of the package directory (None when not a git repo — QCU's
    # repo currently isn't under git, which is itself useful diagnostic data).
    info["git_commit"] = None
    info["git_error"] = None
    try:
        pkg_src = _P(_qcu_pkg.__file__).resolve().parent
        out = subprocess.run(
            ["git", "-C", str(pkg_src), "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=2.0,
        )
        if out.returncode == 0:
            info["git_commit"] = out.stdout.strip()
        else:
            info["git_error"] = out.stderr.strip() or "not a git repository"
    except Exception as e:  # noqa: BLE001
        info["git_error"] = f"{type(e).__name__}: {e}"
    return info


def doctor() -> int:
    """Probe macOS TCC permissions and surface missing ones.

    Also reports installation paths, version and git commit. This is what
    makes drift between the skill copy an agent reads and the editable install
    an agent runs visible — previously both reported ``0.1.0`` while diverging
    in functionality (one had go_back/daemon, one didn't).
    """
    from qcu.layers.runtime import capability_report
    report = permissions.probe_all() if sys.platform == "darwin" else {}
    report["_desktop_backend"] = capability_report()
    report["_install"] = _doctor_install_info()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    _emit_missing_permission_hints(report)
    # Best-effort: open the relevant Privacy panes for anything missing,
    # matching the session-start preflight behaviour.
    for key, label in _PERM_LABELS.items():
        entry = report.get(key, {})
        if entry.get("granted") is False and entry.get("url"):
            try:
                import subprocess
                subprocess.run(["open", entry["url"]], check=False, capture_output=True)
            except Exception:  # noqa: BLE001
                pass
    return 0


# ---------------------------------------------------------------------------
# Browser daemon diagnostics
# ---------------------------------------------------------------------------


def browser_status() -> int:
    """Report whether the Chromium daemon is alive, without touching the session.

    Useful when ``session.json`` is stale or gone: reads pid/port from the
    session if present, and probes the CDP endpoint directly.
    """
    from qcu.layers.browser_daemon import is_cdp_alive, is_pid_alive

    s = load()
    pid = s.browser_pid if s is not None else None
    port = s.browser_debug_port if s is not None else None
    alive = bool(port) and is_cdp_alive(port)
    pid_alive = is_pid_alive(pid) if pid else False
    print(
        json.dumps(
            {
                "alive": alive,
                "port": port,
                "pid": pid,
                "pid_alive": pid_alive,
                "headless": s.headless if s is not None else None,
                "user_data_dir": s.user_data_dir if s is not None else None,
                "note": (
                    "CDP endpoint responding"
                    if alive
                    else (
                        "no daemon recorded"
                        if not port
                        else "port recorded but endpoint not responding (daemon crashed?)"
                    )
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def browser_stop() -> int:
    """Force-stop the Chromium daemon.

    Prefer ``qcu session end`` for a normal shutdown (it stops the daemon AND
    clears the session). Use this when the session file is gone but a daemon
    is still running, or to recover from a wedged state. Reads pid/port from
    the session if available; otherwise reports nothing to stop.
    """
    from qcu.layers.browser_daemon import kill_pid, is_pid_alive

    s = load()
    pid = s.browser_pid if s is not None else None
    if not pid:
        print(json.dumps({"ok": True, "stopped": False, "reason": "no daemon pid recorded"}))
        return 0
    was_alive = is_pid_alive(pid)
    kill_pid(pid)
    # Clear the now-dead browser's metadata so the next observe knows to
    # relaunch, instead of trying to attach to a dead endpoint / dead refs.
    from qcu.session import patch as _patch

    _patch(browser_pid=None, browser_debug_port=None, browser_status="stopped", last_refs=None)
    print(
        json.dumps(
            {"ok": True, "stopped": was_alive, "pid": pid, "still_alive": is_pid_alive(pid)},
            ensure_ascii=False,
        )
    )
    return 0


# ---------------------------------------------------------------------------
# Resident Python CLI daemon diagnostics
# ---------------------------------------------------------------------------


def daemon_status() -> int:
    """Report whether the resident Python daemon is up and what it's holding."""
    from qcu.daemon.server import health_check
    from qcu.session import is_daemon_alive

    s = load()
    pid = s.daemon_pid if s is not None else None
    port = s.daemon_port if s is not None else None
    alive = bool(port) and health_check(port, timeout=1.5) if port else False

    # Ask the daemon which layer instances it's keeping warm — that's the whole
    # point, so surface it when available.
    instances: list[dict[str, Any]] = []
    if alive:
        try:
            from qcu.daemon.server import call_daemon
            resp = call_daemon(port, "daemon_status", {}, timeout=3.0)
            if resp.get("ok"):
                instances = (resp.get("result") or {}).get("instances", [])
        except Exception:
            pass
    print(
        json.dumps(
            {
                "alive": alive,
                "port": port,
                "pid": pid,
                "instances": instances,
                "note": (
                    "daemon healthy" if alive else
                    "no daemon recorded" if not port else
                    "port recorded but daemon not responding"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def daemon_stop() -> int:
    """Force-stop the resident daemon (lighter than session end — keeps session,
    doesn't close the browser). Use to recover from a wedged daemon."""
    from qcu.layers.browser_daemon import kill_pid, is_pid_alive

    s = load()
    pid = s.daemon_pid if s is not None else None
    if not pid:
        print(json.dumps({"ok": True, "stopped": False, "reason": "no daemon pid recorded"}))
        return 0
    was_alive = is_pid_alive(pid)
    kill_pid(pid)
    # Clear the recorded port so the next command lazy-starts a fresh daemon
    # instead of trying to talk to the dead one.
    try:
        from qcu.session import patch
        patch(daemon_pid=None, daemon_port=None)
    except Exception:
        pass
    print(
        json.dumps(
            {"ok": True, "stopped": was_alive, "pid": pid, "still_alive": is_pid_alive(pid)},
            ensure_ascii=False,
        )
    )
    return 0
