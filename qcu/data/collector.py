"""Telemetry — JSONL log of routing decisions.

Every observe() / act() call writes one line to ``~/.qcu/telemetry.jsonl``
(or wherever ``$QCU_HOME`` points). The file is append-only and never read
in-band; use ``qcu stats`` to summarize.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from qcu.router.classifier import RoutingDecision
from qcu.session import _qcu_home, TELEMETRY_PATH


@dataclass
class TelemetryRecord:
    ts: float
    context: str
    features: dict[str, Any]
    decision: RoutingDecision
    action: Optional[dict[str, Any]]
    outcome: Optional[str]
    latency_ms: Optional[float]
    requested_layer: Optional[str] = None
    actual_layer: Optional[str] = None
    verification: Any = None
    timing: Optional[dict[str, Any]] = None


class Telemetry:
    """Static façade. We avoid holding a process-global handle because the
    CLI is short-lived (one process per invocation)."""

    @staticmethod
    def record(
        *,
        context: str,
        features: dict[str, Any],
        decision: RoutingDecision,
        action: Optional[dict[str, Any]],
        outcome: Optional[str],
        latency_ms: Optional[float],
        requested_layer: Optional[str] = None,
        actual_layer: Optional[str] = None,
        verification: Any = None,
        timing: Optional[dict[str, Any]] = None,
    ) -> None:
        rec = TelemetryRecord(
            ts=time.time(),
            context=context,
            features=features,
            decision=decision,
            action=action,
            outcome=outcome,
            latency_ms=latency_ms,
            requested_layer=requested_layer,
            actual_layer=actual_layer,
            verification=verification,
            timing=timing or ({"total_ms": latency_ms} if latency_ms is not None else None),
        )
        path = TELEMETRY_PATH
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(_record_to_dict(rec), ensure_ascii=False)
        # O_APPEND so concurrent processes (e.g. when the LLM spawns many
        # bash calls) don't clobber each other.
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def _record_to_dict(rec: TelemetryRecord) -> dict[str, Any]:
    return {
        "ts": rec.ts,
        "context": rec.context,
        "features": rec.features,
        "decision": rec.decision.to_dict(),
        "action": rec.action,
        "outcome": rec.outcome,
        "latency_ms": rec.latency_ms,
        "requested_layer": rec.requested_layer,
        "decision_layer": rec.decision.layer,
        "actual_layer": rec.actual_layer,
        "verification": rec.verification,
        "timing": rec.timing,
    }


# ---------------------------------------------------------------------------
# Stats summarization
# ---------------------------------------------------------------------------

_SINCE_RE = re.compile(r"^(\d+)([smhd])$")


def _parse_since(s: str, now: float) -> float:
    """Parse a duration like '7d' / '24h' / '30m' into a unix timestamp."""
    m = _SINCE_RE.match(s.strip())
    if not m:
        return 0.0
    n = int(m.group(1))
    unit = m.group(2)
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return now - n * mult


def summary(*, since: str = "7d", path: Optional[str] = None) -> dict[str, Any]:
    """Aggregate the telemetry file by layer + outcome."""
    p = Path(path) if path else TELEMETRY_PATH
    if not p.exists():
        return {"path": str(p), "records": 0, "by_layer": {}, "by_outcome": {}}

    cutoff = _parse_since(since, time.time())
    by_layer: dict[str, dict[str, Any]] = {}
    by_outcome: dict[str, int] = {}
    n_records = 0
    n_since = 0

    with open(p, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            n_records += 1
            if rec.get("ts", 0) < cutoff:
                continue
            n_since += 1
            # Stats by the layer that ACTUALLY ran (an explicit --layer override
            # used to be misattributed to decision.layer, hiding fallbacks and
            # overrides). Fall back to decision.layer only for legacy rows.
            layer = rec.get("actual_layer") or rec.get("decision_layer") or rec.get("decision", {}).get("layer", "?")
            outcome = rec.get("outcome") or "observe"
            lat = rec.get("latency_ms")
            slot = by_layer.setdefault(
                layer,
                {"hits": 0, "ok": 0, "fail": 0, "errors": 0, "latencies_ms": []},
            )
            slot["hits"] += 1
            if outcome == "ok":
                slot["ok"] += 1
            elif outcome == "fail":
                slot["fail"] += 1
            elif outcome == "error":
                slot["errors"] += 1
            by_outcome[outcome] = by_outcome.get(outcome, 0) + 1
            if isinstance(lat, (int, float)):
                slot["latencies_ms"].append(float(lat))

    # Aggregate latency stats.
    for layer, slot in by_layer.items():
        lats = slot.pop("latencies_ms", [])
        if lats:
            lats_sorted = sorted(lats)
            slot["p50_ms"] = round(_percentile(lats_sorted, 50), 2)
            slot["p95_ms"] = round(_percentile(lats_sorted, 95), 2)
            slot["avg_ms"] = round(sum(lats) / len(lats), 2)
        else:
            slot["p50_ms"] = None
            slot["p95_ms"] = None
            slot["avg_ms"] = None

    return {
        "path": str(p),
        "since": since,
        "total_records": n_records,
        "in_window": n_since,
        "by_layer": by_layer,
        "by_outcome": by_outcome,
    }


def _percentile(sorted_values: list[float], p: float) -> float:
    if not sorted_values:
        return 0.0
    k = (len(sorted_values) - 1) * (p / 100.0)
    f = int(k)
    c = min(f + 1, len(sorted_values) - 1)
    if f == c:
        return sorted_values[f]
    return sorted_values[f] + (sorted_values[c] - sorted_values[f]) * (k - f)