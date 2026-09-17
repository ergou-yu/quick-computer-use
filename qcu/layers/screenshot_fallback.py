"""Target-bound screenshot and coordinate fallback.

A fallback borrows the active session's existing target. It never creates a
browser, changes application, or retries an action through another transport.
Grounding coordinates are image pixels; explicit coordinates use the selected
backend's native coordinate space (CSS pixels for web, screen points for AX).
"""
from __future__ import annotations

import math
import os
import uuid
from typing import Any, Optional

from qcu.common.types import Action, LayerResult, Observation
from qcu.grounding import get as get_grounding
from qcu.layers.base import Layer
from qcu.layers.runtime import register


class _TargetError(RuntimeError):
    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


@register("screenshot_fallback")
class ScreenshotFallbackLayer(Layer):
    name = "screenshot_fallback"

    def __init__(self) -> None:
        self._artifacts_dir: Optional[str] = None
        self._capture_meta: dict[str, Any] = {}
        self._last_error: Optional[_TargetError] = None

    def _artifacts_path(self) -> str:
        if self._artifacts_dir is None:
            from qcu.session import _qcu_home
            self._artifacts_dir = os.environ.get("QCU_ARTIFACTS_DIR", str(_qcu_home() / "artifacts"))
            os.makedirs(self._artifacts_dir, exist_ok=True)
        return self._artifacts_dir

    def _target(self, options: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        from qcu.session import load
        from qcu.layers.runtime import get_layer
        s = load()
        if s is None or s.context not in {"web", "desktop"}:
            raise _TargetError("target_unbound", "start an explicit web or desktop session and observe its target first")
        target: dict[str, Any] = {"context": s.context, "session_id": s.session_id}
        if s.context == "web":
            try:
                web = get_layer("web_a11y")
            except Exception as exc:
                raise _TargetError("missing_capability", f"web_a11y is unavailable: {exc}") from exc
            page, loop = getattr(web, "_page", None), getattr(web, "_loop", None)
            if page is None or loop is None or page.is_closed():
                raise _TargetError("missing_capability", "the web session has no connected page; observe the selected web target first")
            try:
                document = loop.run_until_complete(page.evaluate("() => performance.timeOrigin"))
            except Exception as exc:
                raise _TargetError("target_unavailable", f"cannot confirm the current web document: {exc}") from exc
            if document is None:
                raise _TargetError("target_unavailable", "the web document identity is unavailable")
            target.update(backend=web, layer="web_a11y", page=page, loop=loop,
                          identity={"url": page.url, "document": document, "page": id(page)})
        else:
            from qcu.platforms import desktop_backend_name
            name = s.layer if s.layer.startswith("desktop_") else desktop_backend_name()
            try:
                backend = get_layer(name)
            except Exception as exc:
                raise _TargetError("missing_capability", f"{name} is unavailable: {exc}") from exc
            scope = getattr(backend, "_target_scope", None) or getattr(backend, "current_target", None)
            if not isinstance(scope, dict) or not scope.get("pid") or not scope.get("window_id"):
                raise _TargetError("target_unbound", f"{name} has no bound process and window; observe a specific target first")
            for key, value in (options or {}).items():
                field = {"app": "app", "pid": "pid", "window": "window_title", "window_id": "window_id"}.get(key)
                if field and value is not None and str(scope.get(field)) != str(value):
                    raise _TargetError("target_mismatch", f"{key} differs from the bound target; observe the requested target first")
            target.update(backend=backend, layer=name, identity=dict(scope))
            window = getattr(backend, "_target_window", None)
            inspect_window = getattr(backend, "_window_identity", None)
            if window is not None and callable(inspect_window):
                live = inspect_window(window, scope["pid"], scope.get("window_title", ""))
                if live.get("window_id") != scope["window_id"] or not live.get("bounds"):
                    raise _TargetError("target_unavailable", "cannot confirm the live screenshot window geometry")
                target["geometry"] = dict(live["bounds"])
        return target

    def _check_target(self, target: dict[str, Any]) -> None:
        current = self._target()
        if (current["session_id"] != target["session_id"] or current["context"] != target["context"]
                or current["backend"] is not target["backend"] or current["identity"] != target["identity"]
                or current.get("geometry") != target.get("geometry")):
            raise _TargetError("target_changed", "session, backend, window or document changed; observe and locate again")

    def _failure(self, error: _TargetError) -> LayerResult:
        return LayerResult(ok=False, layer=self.name, message=str(error),
                           data={"reason": error.reason}, dispatch_state="not_sent")

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        try:
            target = self._target(options)
            path = self._capture(target)
            if path is None:
                raise self._last_error or _TargetError("missing_capability", "target screenshot unavailable")
            return Observation(context=target["context"], url_or_app=target["identity"].get("url") or target["identity"].get("app"),
                elements=[], screenshot_path=path,
                routing_meta={"layer": self.name, "captured": True, **self._capture_meta})
        except _TargetError as exc:
            from qcu.session import load
            s = load()
            return Observation(context=s.context if s else "unknown", elements=[],
                routing_meta={"layer": self.name, "captured": False, "reason": exc.reason, "error": str(exc)})

    def _capture(self, target: Optional[dict[str, Any]] = None, *, path: Optional[str] = None) -> Optional[str]:
        self._last_error = None
        self._capture_meta = {}
        try:
            target = target or self._target()
            self._check_target(target)
            path = path or os.path.join(self._artifacts_path(), f"screenshot-{uuid.uuid4().hex}.png")
            if target["context"] == "web":
                # CSS-scale capture preserves the page.mouse coordinate frame.
                target["loop"].run_until_complete(target["page"].screenshot(path=path, full_page=False, scale="css"))
                metadata = {"coordinate_origin": {"x": 0, "y": 0}, "coordinate_scale": 1.0}
            else:
                screenshot = getattr(target["backend"], "_screenshot", None)
                if screenshot is None:
                    raise _TargetError("missing_capability", f"{target['layer']} does not implement target screenshots")
                result = screenshot(path)
                if not result.ok:
                    raise _TargetError(result.data.get("reason", "missing_capability"), result.message)
                metadata = dict(result.data)
                if metadata.get("target") != target["identity"]:
                    raise _TargetError("target_mismatch", "screenshot backend did not confirm the bound target")
            self._check_target(target)
            if not os.path.isfile(path):
                raise _TargetError("capture_failed", "target backend returned no screenshot file")
            self._capture_meta = {**metadata, "path": path, "target": target["identity"], "backend": target["layer"]}
            return path
        except Exception as exc:
            self._last_error = exc if isinstance(exc, _TargetError) else _TargetError("capture_failed", str(exc))
            return None

    def act(self, action: Action) -> LayerResult:
        if action.type not in {"click", "double_click", "right_click", "hover", "screenshot"}:
            return self._failure(_TargetError("unsupported", f"screenshot fallback does not implement {action.type!r}"))
        if action.params.get("ref"):
            return self._failure(_TargetError("unsupported_reference", "visual fallback cannot reinterpret a control ref; observe and locate the target again"))
        try:
            target = self._target(action.params)
            if action.type == "screenshot":
                path = self._capture(target, path=action.params.get("path"))
                if path is None:
                    raise self._last_error or _TargetError("capture_failed", "could not capture the target")
                return LayerResult(ok=True, layer=self.name, message="target screenshot captured", data=dict(self._capture_meta),
                                   dispatch_state="not_sent", outcome="verified")
            if "x" in action.params and "y" in action.params:
                return self._click_xy(float(action.params["x"]), float(action.params["y"]), action=action, target=target)
            grounding_name = action.params.get("grounding")
            grounding = get_grounding(grounding_name) if grounding_name else None
            if grounding is None:
                raise _TargetError("missing_capability", "provide explicit x,y or an installed grounding model")
            path = self._capture(target)
            if path is None:
                raise self._last_error or _TargetError("capture_failed", "could not capture the target for grounding")
            with open(path, "rb") as stream:
                result = grounding.ground(stream.read(), query=action.params.get("query") or action.type)
            if len(result.coords) != 1:
                raise _TargetError("ambiguous" if result.coords else "locate_failed", "grounding must return exactly one target coordinate")
            origin = self._capture_meta.get("coordinate_origin")
            scale = self._capture_meta.get("coordinate_scale")
            if not isinstance(origin, dict) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0:
                raise _TargetError("missing_capability", "screenshot does not declare its coordinate origin and scale")
            x = float(origin["x"]) + float(result.coords[0]["x"]) / scale
            y = float(origin["y"]) + float(result.coords[0]["y"]) / scale
            return self._click_xy(x, y, action=action, target=target)
        except (ValueError, TypeError, KeyError) as exc:
            return self._failure(_TargetError("invalid_coordinates", str(exc)))
        except _TargetError as exc:
            return self._failure(exc)
        except Exception as exc:
            # Capture/grounding errors occur before _click_xy owns dispatch.
            return self._failure(_TargetError("grounding_failed", str(exc)))

    def _click_xy(self, x: float, y: float, *, action: Optional[Action] = None,
                  target: Optional[dict[str, Any]] = None) -> LayerResult:
        try:
            if not math.isfinite(x) or not math.isfinite(y):
                raise _TargetError("invalid_coordinates", "coordinates must be finite")
            target = target or self._target()
            self._check_target(target)
            atype = action.type if action else "click"
            params = dict(action.params) if action else {}
            params.update(x=x, y=y)
            routed = Action(type=atype, params=params)
            if target["context"] == "web":
                operation = target["backend"]._act_async(routed)
                run = lambda: target["loop"].run_until_complete(operation)
            else:
                if not callable(getattr(target["backend"], "_click", None)):
                    raise _TargetError("missing_capability", f"{target['layer']} does not support coordinate actions")
                run = lambda: target["backend"].act(routed)
        except _TargetError as exc:
            return self._failure(exc)
        try:
            result = run()
            result.data.setdefault("target", target["identity"])
            result.data["fallback_layer"] = self.name
            return result
        except Exception as exc:
            # The attempted backend might already have delivered the event.
            # Never replay through desktop or any other transport.
            return LayerResult(ok=False, layer=self.name, message=f"coordinate dispatch outcome unknown: {exc}",
                data={"reason": "outcome_unknown", "retry_safe": False, "target": target["identity"]},
                dispatch_state="unknown", outcome="unknown")

    def close(self) -> None:
        self._capture_meta = {}
