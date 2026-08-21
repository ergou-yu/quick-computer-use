"""Grounding model interface.

A grounding model maps ``(screenshot_bytes, prompt)`` to either
coordinates or a structured element reference. Common choices:

- **OmniParser** (Microsoft) — YOLO-based icon/text detector.
- **Florence-2** (Microsoft) — phrase-grounding VLM.
- **Set-of-Mark (SoM)** — GPT-4V marks a screenshot with numbered tags.

This MVP ships only the Protocol. Implementations live outside the core
package and can be plugged in via ``GroundingRegistry.register(...)``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Protocol, runtime_checkable


@dataclass
class GroundingResult:
    """Result of a grounding query.

    Either ``coords`` (a list of {x, y, confidence, label}) or ``refs`` (a
    list of stable element handles the caller can act on) is populated.
    """

    coords: list[dict[str, Any]]
    refs: list[dict[str, Any]]
    raw: Any = None


@runtime_checkable
class GroundingModel(Protocol):
    """Maps a screenshot to actionable element coordinates or refs."""

    name: str

    def ground(
        self,
        screenshot_bytes: bytes,
        *,
        query: str = "",
        max_results: int = 5,
    ) -> GroundingResult:
        ...


_REGISTRY: dict[str, type[GroundingModel]] = {}


def register(name: str) -> Any:
    def deco(cls: type[GroundingModel]) -> type[GroundingModel]:
        _REGISTRY[name] = cls
        return cls

    return deco


def get(name: str) -> Optional[GroundingModel]:
    cls = _REGISTRY.get(name)
    if cls is None:
        return None
    return cls()


def available() -> list[str]:
    return sorted(_REGISTRY.keys())