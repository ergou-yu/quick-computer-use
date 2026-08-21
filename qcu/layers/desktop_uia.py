"""Windows UIA layer — interface stub only.

On Windows the natural choice is ``uiautomation`` (a native wrapper around
Microsoft UI Automation COM) or ``pywinauto`` (UIA backend). Both expose
``Control`` objects with ``Name``, ``ControlTypeName``, ``BoundingRectangle``,
and ``Click()`` / ``SetValue()`` / ``SendKeys()``.

The MVP ships only the interface (``DesktopUIALayer``) so cross-platform
code can compile and tests can mock. To enable, install
``uiautomation>=2.0`` and uncomment the implementation below.
"""

from __future__ import annotations

from typing import Any, Optional

from qcu.common.types import Action, LayerResult, Observation
from qcu.layers.base import Layer
from qcu.layers.runtime import register


@register("desktop_uia")
class DesktopUIALayer(Layer):
    name = "desktop_uia"

    def __init__(self) -> None:
        self._available = False
        try:
            import uiautomation  # type: ignore  # noqa: F401

            self._available = True
        except Exception:
            self._available = False

    def observe(self, max_depth: int = 8) -> Observation:  # noqa: ARG002
        if not self._available:
            return Observation(
                context="desktop",
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "available": False,
                    "reason": "uiautomation not installed (Windows-only backend)",
                },
            )
        # Implementation sketch (left as v2):
        #   from uiautomation import GetRootControl, Control
        #   root = GetRootControl()
        #   for ctrl in root.GetChildren():
        #       role = from_uia(ctrl.ControlTypeName)
        #       ...
        return Observation(
            context="desktop",
            elements=[],
            routing_meta={
                "layer": self.name,
                "available": True,
                "implemented": False,
                "reason": "MVP stub — install + implement in v2",
            },
        )

    def act(self, action: Action) -> LayerResult:
        return LayerResult(
            ok=False,
            layer=self.name,
            message="desktop_uia is an MVP stub; not implemented",
        )

    def close(self) -> None:
        return None