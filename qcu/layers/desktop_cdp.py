"""Desktop CDP bridge — drive Electron/CEF desktop apps through their own DOM.

Modern Chromium/Electron apps keep renderer accessibility OFF at runtime: the
AX tree shows only the menu bar and window chrome (real-machine probes against
Cursor and Qianwen in 1.9). When the app was launched with
``--remote-debugging-port``, this layer attaches to that CDP endpoint and
delegates to the web_a11y machinery — full DOM semantics at web speed, no
screenshots, no visual grounding.

Honesty rules inherited from the reliability hardening:

- The endpoint must belong to the *bound* pid (``_cdp_bridge`` only probes
  ports that process listens on); a missing endpoint is reported with a
  relaunch hint, never silently replaced by another target.
- Page selection refuses to guess: a ``--window`` title filter must match
  exactly one page; zero/multiple matches fail with the available titles.
- The attach never launches, relaunches, navigates or closes the user's app.
  ``close()`` drops only the local Playwright driver.

Usage:

    qcu observe --layer desktop_cdp --pid 12345 [--window '部分标题']
    qcu act '{"type":"click","params":{"ref":"..."}}'   # router keeps the layer
"""
from __future__ import annotations

from typing import Any, Optional

from qcu.common.types import Action, LayerResult, Observation
from qcu.layers.base import Layer
from qcu.layers.runtime import register

# Options that web_a11y.observe understands and that make sense for a bridged
# page. Everything else (app/pid/window) is consumed by this layer itself.
_WEB_OBSERVE_KEYS = (
    "full_text", "text_limit", "tail", "limit", "offset", "compact",
    "roles", "query", "wait_until", "wait_for", "timeout_ms",
)


@register("desktop_cdp")
class DesktopCDPLayer(Layer):
    name = "desktop_cdp"

    def __init__(self) -> None:
        self._bound: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Target resolution
    # ------------------------------------------------------------------

    def _resolve_pid(self, app: Optional[str], pid: Optional[int]) -> tuple[Optional[int], Optional[LayerResult], str]:
        """Resolve the target pid. Explicit pid wins; an app name must match
        exactly one running process (never an arbitrary pick)."""
        if pid is not None:
            try:
                pid = int(pid)
                if pid <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                return None, LayerResult(ok=False, layer=self.name,
                                         message="Explicit pid must be a positive integer.",
                                         data={"reason": "invalid_target"}, dispatch_state="not_sent"), ""
            return pid, None, app or ""
        if app:
            try:
                from AppKit import NSWorkspace  # type: ignore
            except Exception as exc:  # noqa: BLE001
                return None, LayerResult(ok=False, layer=self.name,
                                         message=f"App-name resolution needs macOS AppKit; pass --pid instead. ({exc})",
                                         data={"reason": "missing_capability"}, dispatch_state="not_sent"), ""
            wanted = app.casefold()
            matches = [
                r for r in NSWorkspace.sharedWorkspace().runningApplications()
                if wanted in str(r.localizedName() or "").casefold()
                or wanted in str(r.bundleIdentifier() or "").casefold()
            ]
            if len(matches) != 1:
                return None, LayerResult(
                    ok=False, layer=self.name,
                    message="Application is missing or ambiguous; pass a unique --pid.",
                    data={"reason": "ambiguous_app" if matches else "app_not_found",
                          "candidates": [{"pid": int(m.processIdentifier()),
                                          "app": str(m.localizedName() or "")} for m in matches]},
                    dispatch_state="not_sent"), ""
            return int(matches[0].processIdentifier()), None, str(matches[0].localizedName() or app)
        return None, LayerResult(ok=False, layer=self.name,
                                 message="desktop_cdp needs an explicit --pid or --app.",
                                 data={"reason": "invalid_target"}, dispatch_state="not_sent"), ""

    def _web(self) -> Any:
        from qcu.layers.runtime import get_layer
        return get_layer("web_a11y")

    # ------------------------------------------------------------------
    # Layer interface
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        from qcu.layers import _cdp_bridge

        app = options.get("app")
        pid = options.get("pid")
        if pid is None and app is None and self._bound:
            pid, app = self._bound.get("pid"), self._bound.get("app")
        pid, error, resolved_app = self._resolve_pid(app, pid)
        if error is not None:
            return Observation(context="desktop", elements=[], routing_meta={
                "layer": self.name, "error": error.message, **error.data})

        endpoint, diag = _cdp_bridge.find_cdp_endpoint(pid)
        if endpoint is None:
            return Observation(
                context="desktop", url_or_app=resolved_app or str(pid), elements=[],
                routing_meta={"layer": self.name, "error": "CDP endpoint not available for this app.",
                              "reason": diag.get("reason", "no_cdp_endpoint"),
                              "cdp": diag, "hint": diag.get("hint")})

        title_hint = str(options.get("window") or "")
        web = self._web()
        try:
            web.attach_external_cdp(endpoint["url"], pid=pid, app=resolved_app, title_hint=title_hint)
        except RuntimeError as exc:
            # _select_external_page errors arrive as a JSON message.
            try:
                import json
                detail = json.loads(str(exc))
            except (ValueError, TypeError):
                detail = {"reason": "attach_failed", "message": str(exc)}
            return Observation(context="desktop", url_or_app=resolved_app or str(pid), elements=[],
                               routing_meta={"layer": self.name, "error": detail.get("message", str(exc)),
                                             **{k: v for k, v in detail.items() if k != "message"},
                                             "cdp": {"endpoint": endpoint["url"], "browser": endpoint["browser"]}})

        web_options = {k: options[k] for k in _WEB_OBSERVE_KEYS if k in options and options[k] is not None}
        obs = web.observe(max_depth=max_depth, **web_options)

        self._bound = {"pid": pid, "app": resolved_app, "endpoint": endpoint["url"],
                       "title_hint": title_hint}
        obs.context = "desktop"
        meta = dict(obs.routing_meta)
        meta["layer"] = self.name
        meta["bridged_via"] = "web_a11y"
        meta["cdp"] = {"endpoint": endpoint["url"], "browser": endpoint["browser"],
                       "pid": pid, "app": resolved_app, "title_hint": title_hint}
        obs.routing_meta = meta
        return obs

    def act(self, action: Action) -> LayerResult:
        if not self._bound:
            return LayerResult(ok=False, layer=self.name,
                               message="No CDP target bound; observe the app first.",
                               data={"reason": "target_unbound"}, dispatch_state="not_sent")
        web = self._web()
        external = getattr(web, "_external", None)
        if not external or external.get("endpoint") != self._bound["endpoint"]:
            return LayerResult(ok=False, layer=self.name,
                               message="The CDP attachment changed or was lost; observe the target again.",
                               data={"reason": "target_changed"}, dispatch_state="not_sent")
        result = web.act(action)
        result.data.setdefault("cdp", {"endpoint": self._bound["endpoint"], "pid": self._bound["pid"]})
        result.data["bridged_via"] = "web_a11y"
        result.layer = self.name
        return result

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "desktop_cdp",
            "implemented": True,
            "requires": "target app launched with --remote-debugging-port (Electron/CEF)",
            "bound": bool(self._bound),
            "reads_controls": True,
            "actions": ["click", "double_click", "right_click", "hover", "fill", "type",
                        "press_key", "scroll", "wait", "screenshot"],
            "verifies_results": True,
            "needs_foreground_input": False,
        }

    def close(self) -> None:
        self._bound = {}
