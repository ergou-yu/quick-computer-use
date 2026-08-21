"""Feature extraction from an Observation.

The classifier only sees this feature dict — never the raw Observation.
Keeping the schema flat and JSON-friendly makes data collection trivial.
"""

from __future__ import annotations

from typing import Any, Optional

from qcu.common.types import Action, Observation
from qcu.common.normalize import INTERACTIVE_ROLES, NORMALIZED_ROLES


def extract(obs: Observation, *, last_action: Optional[Action] = None) -> dict[str, Any]:
    """Compute the routing-relevant features of the current observation.

    Returns a flat dict of booleans, counts, and strings. Keys with ``None``
    values are dropped so JSONL stays tidy.
    """
    n = len(obs.elements)
    has_canvas = any(e.role == "canvas" for e in obs.elements)
    has_webmcp = bool(obs.tools)
    webmcp_tool_names = {t.get("name") for t in obs.tools if isinstance(t, dict)}

    # Embedded web view (WKWebView/Electron) detection. Two signals, OR'd so
    # the feature stays true whichever layer produced the observation:
    #   1. routing_meta["web_view"] — the authoritative flag set by
    #      desktop_ax / desktop_appleevents observe when the walk hits an
    #      AXWebArea node.
    #   2. an element whose raw_role is "AXWebArea" (defensive: a layer that
    #      surfaces the web-area as an Element still trips the feature even
    #      if it forgot to set routing_meta).
    # This is the signal the desktop_webview_blind rule keys on; it tells the
    # LLM "this desktop app is an opaque web-view shell — escalate to T3".
    has_web_area = bool(obs.routing_meta.get("web_view")) or any(
        (e.properties or {}).get("raw_role") == "AXWebArea"
        for e in obs.elements
    )

    # A full browser (Chrome/Safari/Edge/Arc/...) detected via AppleScript
    # tab probing. Independent of has_web_area: a stock Chrome window does
    # NOT expose AXWebArea unless AXManualAccessibility is flipped, so
    # has_web_area is False for Chrome even though the DOM is just as opaque.
    # The router needs this second signal to fire the T3 takeover hint for a
    # real browser window (the CRM-in-Chrome case).
    has_web_app = bool(obs.routing_meta.get("web_app"))

    roles: dict[str, int] = {}
    for e in obs.elements:
        roles[e.role] = roles.get(e.role, 0) + 1

    feats: dict[str, Any] = {
        "context": obs.context,
        "url_or_app": obs.url_or_app,
        "n_elements": n,
        "n_interactive": sum(
            1 for e in obs.elements if e.role in INTERACTIVE_ROLES
        ),
        "roles": roles,
        "has_canvas": has_canvas,
        "has_web_area": has_web_area,
        "has_web_app": has_web_app,
        "has_webmcp": has_webmcp,
        "webmcp_tool_count": len(obs.tools),
        "webmcp_tool_names": sorted(t for t in webmcp_tool_names if isinstance(t, str)),
        "has_screenshot": obs.screenshot_path is not None,
        "has_raw_tree": obs.raw_tree is not None and len(obs.raw_tree) > 0,
    }

    # Action-derived features
    if last_action is not None:
        feats["action_type"] = last_action.type
        feats["action_params"] = last_action.params
        ref = last_action.params.get("ref") if isinstance(last_action.params, dict) else None
        feats["action_ref"] = ref
        feats["target_in_a11y"] = isinstance(ref, str) and any(e.ref == ref for e in obs.elements)
        feats["action_in_webmcp_tools"] = (
            last_action.type == "webmcp_call"
            or (
                isinstance(last_action.params, dict)
                and last_action.params.get("tool") in webmcp_tool_names
            )
        )
        # Critical actions always want a visual confirmation pass.
        feats["action_in_critical_list"] = last_action.type in {
            "submit_payment",
            "delete_account",
            "confirm_purchase",
        }
        feats["needs_visual_confirm"] = feats["action_in_critical_list"]
        # Canvas-targeted click.
        if (
            isinstance(ref, str)
            and not feats["target_in_a11y"]
            and has_canvas
        ):
            feats["has_canvas_target"] = True
        else:
            feats["has_canvas_target"] = False
    else:
        feats.setdefault("target_in_a11y", False)
        feats.setdefault("action_in_webmcp_tools", False)
        feats.setdefault("action_in_critical_list", False)
        feats.setdefault("needs_visual_confirm", False)
        feats.setdefault("has_canvas_target", False)

    return {k: v for k, v in feats.items() if v is not None}


def normalize_role_counts(feats: dict[str, Any]) -> dict[str, int]:
    """Helper used by tests: return only the role histogram."""
    r = feats.get("roles")
    if isinstance(r, dict):
        return {k: int(v) for k, v in r.items() if k in NORMALIZED_ROLES}
    return {}