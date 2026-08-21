"""Common dataclasses shared across all layers.

These types are the contract between the Router, the layers, and the CLI.
They are deliberately JSON-serializable so the LLM can read them directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------


@dataclass
class Rect:
    """Axis-aligned rectangle in screen points (web) or screen points (macOS).

    Note: macOS AX reports positions in points (same unit Quartz mouse events
    accept), so no scale math is needed on Retina displays.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def cx(self) -> float:
        return self.x + self.width / 2.0

    @property
    def cy(self) -> float:
        return self.y + self.height / 2.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Element
# ---------------------------------------------------------------------------


@dataclass
class Element:
    """A single interactive element surfaced to the LLM.

    ``ref`` is the stable handle the LLM passes back in ``Action``. It is
    unique within one Observation; if the tree changes, refs are re-mapped.
    """

    ref: str
    role: str  # normalized role, see qcu.common.normalize
    name: Optional[str] = None
    value: Optional[str] = None
    enabled: bool = True
    focused: bool = False
    bounds: Optional[Rect] = None
    backend_id: Optional[str] = None  # CDP backendDOMNodeId / AX UIElement ref
    properties: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # asdict() on a nested dataclass already handles Rect, but normalize to floats.
        if self.bounds is not None:
            d["bounds"] = self.bounds.to_dict()
        return d


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """A snapshot of the current environment, ready for the LLM."""

    context: str  # "web" | "desktop"
    url_or_app: Optional[str] = None  # URL for web, app name for desktop
    title: Optional[str] = None
    elements: list[Element] = field(default_factory=list)
    routing_meta: dict[str, Any] = field(default_factory=dict)
    raw_tree: Optional[str] = None  # text dump for LLM (a11y tree, AX tree, ...)
    screenshot_path: Optional[str] = None  # only present if layer chose screenshot
    tools: list[dict[str, Any]] = field(default_factory=list)  # WebMCP tools if present

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "url_or_app": self.url_or_app,
            "title": self.title,
            "elements": [e.to_dict() for e in self.elements],
            "routing_meta": self.routing_meta,
            "raw_tree": self.raw_tree,
            "screenshot_path": self.screenshot_path,
            "tools": self.tools,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Observation":
        """Reconstruct an observation from its JSON representation.

        Unknown top-level and element fields are ignored for forward compatibility.
        This is intentionally the inverse of :meth:`to_dict` so a real observation
        can be persisted between short-lived CLI invocations and reused for routing.
        """
        if not isinstance(data, dict):
            raise ValueError("Observation must be an object")
        elements: list[Element] = []
        element_fields = set(Element.__dataclass_fields__)
        for raw in data.get("elements") or []:
            if not isinstance(raw, dict):
                continue
            values = {k: v for k, v in raw.items() if k in element_fields}
            bounds = values.get("bounds")
            if isinstance(bounds, dict):
                try:
                    values["bounds"] = Rect(**{k: bounds[k] for k in ("x", "y", "width", "height")})
                except (KeyError, TypeError, ValueError):
                    values["bounds"] = None
            try:
                elements.append(Element(**values))
            except TypeError:
                continue
        return cls(
            context=str(data.get("context") or "web"),
            url_or_app=data.get("url_or_app"),
            title=data.get("title"),
            elements=elements,
            routing_meta=dict(data.get("routing_meta") or {}),
            raw_tree=data.get("raw_tree"),
            screenshot_path=data.get("screenshot_path"),
            tools=list(data.get("tools") or []),
        )

    @classmethod
    def from_json(cls, value: str) -> "Observation":
        return cls.from_dict(json.loads(value))

    def find_ref(self, ref: str) -> Optional[Element]:
        for e in self.elements:
            if e.ref == ref:
                return e
        return None

    def find(
        self,
        *,
        role: Optional[str] = None,
        name: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[Element]:
        """Return elements matching role and case-insensitive name substring."""
        needle = name.casefold() if name else None
        matches = [
            element
            for element in self.elements
            if (role is None or element.role == role)
            and (needle is None or needle in (element.name or "").casefold())
        ]
        start = max(0, offset)
        return matches[start:] if limit is None else matches[start : start + max(0, limit)]


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------


# Reserved action types. The CLI also accepts an opaque string for
# layer-specific actions (e.g. "webmcp_call" with arbitrary params).
ACTION_TYPES = frozenset(
    {
        "click",  # params: {ref: str} OR {x: float, y: float}
        "double_click",
        "hover",
        "type",  # params: {text: str}  (types into currently focused element)
        "fill",  # params: {ref: str, text: str}
        "press_key",  # params: {key: str}  (e.g. "Enter", "Tab", "Escape", "Cmd+Q")
        "scroll",  # params: {dx: int, dy: int}
        "navigate",  # params: {url: str}
        "go_back",  # params: {} — browser-level back (preserves list state, no reload)
        "go_forward",  # params: {}
        "right_click",  # params: {ref} or {x, y} — desktop context menu
        "drag",  # params: {ref_from, ref_to} | {from:{x,y}, to:{x,y}} | {x1,y1,x2,y2, duration?, steps?}
        "launch_app",  # params: {app: str} — open an app by name
        "activate_app",  # params: {app: str} — bring a running app to front
        "wait",  # params: {ms: int}
        "screenshot",  # params: {path?: str}
        "webmcp_call",  # params: {tool: str, args: dict}
    }
)

# Action types that should always trigger a visual confirmation screenshot
# before execution (configurable per-session).
DEFAULT_CRITICAL_ACTIONS = frozenset(
    {
        "submit_payment",
        "delete_account",
        "confirm_purchase",
    }
)


@dataclass
class Action:
    """An instruction the LLM sends to the system."""

    type: str
    params: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "params": self.params}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_json(cls, s: str) -> "Action":
        d = json.loads(s)
        if not isinstance(d, dict) or "type" not in d:
            raise ValueError(f"Action JSON must be an object with 'type': got {d!r}")
        return cls(type=d["type"], params=d.get("params") or {})


# ---------------------------------------------------------------------------
# Layer result
# ---------------------------------------------------------------------------


@dataclass
class LayerResult:
    """Result returned by ``Layer.act``."""

    ok: bool
    layer: str  # name of the layer that handled this action
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "layer": self.layer, "message": self.message, "data": self.data}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)