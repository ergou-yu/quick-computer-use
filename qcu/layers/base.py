"""Layer Protocol — the contract every control surface implements.

A Layer knows how to ``observe()`` (produce an Observation) and ``act()``
(consume an Action and report the result). Layers may carry process-local
state (e.g. a Playwright browser handle) and live across CLI invocations
via the session file.
"""

from __future__ import annotations

from typing import Any, Optional, Protocol, runtime_checkable

from qcu.common.types import Action, LayerResult, Observation


@runtime_checkable
class Layer(Protocol):
    """One control surface (web a11y, desktop AX, screenshot fallback, ...)."""

    name: str

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        """Capture the current environment into an Observation.

        Layers may support additional filtering/rendering options. Callers must
        feature-detect support so legacy third-party layers remain compatible.
        """

    def act(self, action: Action) -> LayerResult:
        """Perform an action. May raise on transport errors."""

    def close(self) -> None:
        """Release any handles. Safe to call multiple times."""