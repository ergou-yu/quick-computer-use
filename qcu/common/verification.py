"""Small, read-only postconditions shared by native and web backends."""
from __future__ import annotations

from typing import Any, Iterable

from qcu.common.types import Element


def validate_condition(condition: Any) -> None:
    if condition is None:
        return
    if not isinstance(condition, dict):
        raise ValueError("verify must be an object")
    kind = condition.get("kind")
    if kind not in {"value", "checked", "selected", "text"}:
        raise ValueError("verify.kind must be value, checked, selected or text")
    if kind != "text" and not isinstance(condition.get("ref"), str):
        raise ValueError("state verification needs verify.ref")
    if "ref" in condition and not isinstance(condition["ref"], str):
        raise ValueError("verify.ref must be a string")
    if kind in {"checked", "selected"}:
        if type(condition.get("equals")) is not bool:
            raise ValueError("state verification needs a boolean equals")
    elif not isinstance(condition.get("equals", condition.get("contains")), str):
        raise ValueError("value/text verification needs string equals or contains")
    if "equals" in condition and "contains" in condition:
        raise ValueError("use either equals or contains")
    if condition.get("contains") == "" or (kind == "text" and condition.get("equals") == ""):
        raise ValueError("text evidence must be non-empty; an empty value equality is valid only for field values")
    timeout = condition.get("timeout_ms", 1000)
    if type(timeout) is not int or not 0 <= timeout <= 10000:
        raise ValueError("verify.timeout_ms must be an integer in 0..10000")


def match_condition(condition: dict[str, Any], elements: Iterable[Element]) -> dict[str, Any]:
    """Evaluate only values actually exposed by the current target."""
    validate_condition(condition)
    candidates = list(elements)
    ref = condition.get("ref")
    if ref is not None:
        candidates = [el for el in candidates if el.ref == ref]
        if len(candidates) != 1:
            return {"verified": False, "reason": "verification_ref_missing_or_ambiguous"}
    kind = condition["kind"]
    values = []
    for element in candidates:
        if kind == "value":
            values.append(element.value)
        elif kind in {"checked", "selected"}:
            values.append(element.properties.get(kind))
        else:
            values.extend([element.name, element.value])
    if "contains" in condition:
        matched = any(isinstance(v, str) and condition["contains"] in v for v in values)
    else:
        expected = condition["equals"]
        matched = any(type(v) is type(expected) and v == expected for v in values)
    return {"verified": matched, "kind": kind, "condition": condition,
            "reason": "condition_matched" if matched else "condition_not_observed"}
