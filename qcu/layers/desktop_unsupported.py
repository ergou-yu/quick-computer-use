"""Explicit unsupported-platform endpoint; never selects another target."""
from qcu.common.types import Action, LayerResult, Observation
from qcu.layers.runtime import register
from qcu.platforms import desktop_capabilities


@register("desktop_linux")
@register("desktop_harmony")
@register("desktop_unsupported")
class UnsupportedDesktopLayer:
    name = "desktop_unsupported"

    def capabilities(self):
        return desktop_capabilities(layer=self.name)

    def observe(self, max_depth=8, **options):
        cap = self.capabilities()
        return Observation(context="desktop", routing_meta={
            "layer": cap["backend"], "available": False,
            "reason": cap["reason"], "capabilities": cap})

    def act(self, action: Action) -> LayerResult:
        return LayerResult(False, self.capabilities()["backend"],
                           "Desktop backend is not implemented on this platform",
                           {"reason": "missing_capability", "capabilities": self.capabilities()},
                           dispatch_state="not_sent")

    def close(self):
        pass
