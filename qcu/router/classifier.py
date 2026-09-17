"""The Router.

Single entry point: :func:`classify` returns a :class:`RoutingDecision`
describing which layer should handle the next operation and why. It also
records what every other rule would have done, so the LLM (and future ML
training data) can see the alternatives.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

from qcu.common.types import Action
from qcu.router.rules import RULES, default_layer


@dataclass
class RoutingDecision:
    layer: str
    reason: str
    priority: int
    rule_name: str
    alternatives: list[dict[str, Any]] = field(default_factory=list)
    features: dict[str, Any] = field(default_factory=dict)

    def to_dict(self, *, compact: bool = False) -> dict[str, Any]:
        d = asdict(self)
        # Compact mode: drop the per-rule alternative trace (12 rules × ~200B =
        # ~2.4 KB on the typical desktop observe). Useful for telemetry/sys-admin
        # debugging; pure noise for an LLM that just wants to know what won.
        if compact:
            d.pop("alternatives", None)
            d.pop("features", None)
        return d


def classify(
    features: dict[str, Any],
    *,
    last_action: Optional[Action] = None,
) -> RoutingDecision:
    """Walk the rule chain and return the first match (highest priority first)."""
    # Defensive: features may be a JSON-decoded dict from the CLI.
    if not isinstance(features, dict):
        features = {}

    # Compute the alternatives regardless of which rule wins, so telemetry
    # captures the full decision landscape.
    evaluated: list[tuple[int, str, bool, str, str]] = []
    for priority, name, cond, layer, reason in RULES:
        try:
            matched = bool(cond(features))
        except Exception:
            matched = False
        resolved_layer = layer(features) if callable(layer) else layer
        evaluated.append((priority, name, matched, resolved_layer, reason))

    winner: Optional[tuple[int, str, bool, str, str]] = None
    for entry in evaluated:
        if entry[2]:
            winner = entry
            break

    if winner is None:
        layer = default_layer()
        reason = "no rule matched; using safe default"
        priority = -1
        name = "_default"
    else:
        _priority, name, _matched, layer, reason = winner
        priority = _priority

    alternatives = [
        {
            "rule": n,
            "layer": L,
            "matched": m,
            "priority": p,
            "reason": r,
        }
        for p, n, m, L, r in evaluated
        if not (winner is not None and n == winner[1])
    ]

    return RoutingDecision(
        layer=layer,
        reason=reason,
        priority=priority,
        rule_name=name,
        alternatives=alternatives,
        features=features,
    )