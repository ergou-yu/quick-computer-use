"""JSON schemas for the public Observation/Action contract.

These are emitted by ``qcu schema observation`` / ``qcu schema action``.
They are written by hand (no pydantic dep) to keep the install footprint
small and the format readable.
"""

from __future__ import annotations


SCHEMAS: dict[str, dict] = {
    "observation": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Observation",
        "type": "object",
        "required": ["context", "elements"],
        "properties": {
            "context": {
                "type": "string",
                "enum": ["web", "desktop"],
                "description": "Environment the snapshot came from.",
            },
            "url_or_app": {
                "type": ["string", "null"],
                "description": "URL (web) or app name (desktop).",
            },
            "title": {"type": ["string", "null"]},
            "elements": {
                "type": "array",
                "items": {"$ref": "#/$defs/Element"},
                "description": "Interactive elements with stable refs.",
            },
            "routing_meta": {
                "type": "object",
                "description": "Why the router picked this layer and what it considered.",
            },
            "raw_tree": {
                "type": ["string", "null"],
                "description": "Human-readable a11y/AX tree (optional).",
            },
            "screenshot_path": {
                "type": ["string", "null"],
                "description": "Set only when screenshot_fallback was triggered.",
            },
            "tools": {
                "type": "array",
                "description": "WebMCP tools registered by the page, if any.",
            },
            "elapsed_ms": {"type": "number"},
            "router_decision": {"$ref": "#/$defs/RoutingDecision"},
        },
        "$defs": {
            "Element": {
                "type": "object",
                "required": ["ref", "role"],
                "properties": {
                    "ref": {"type": "string", "description": "Stable handle for `act`."},
                    "role": {
                        "type": "string",
                        "description": "Normalized role (button, edit, link, ...).",
                    },
                    "name": {"type": ["string", "null"]},
                    "value": {"type": ["string", "null"]},
                    "enabled": {"type": "boolean"},
                    "focused": {"type": "boolean"},
                    "bounds": {
                        "type": "object",
                        "properties": {
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "width": {"type": "number"},
                            "height": {"type": "number"},
                        },
                    },
                    "backend_id": {"type": ["string", "null"]},
                    "properties": {
                        "type": "object",
                        "description": (
                            "Layer-specific details. For web links this includes "
                            "href, absolute_href, same_origin, target; interactive "
                            "elements may include checked/selected/expanded/pressed/"
                            "readonly/required; raw_role and depth are always present."
                        ),
                        "properties": {
                            "href": {"type": ["string", "null"]},
                            "absolute_href": {"type": ["string", "null"]},
                            "same_origin": {"type": "boolean"},
                            "target": {"type": ["string", "null"]},
                            "depth": {"type": "integer"},
                        },
                    },
                },
            },
            "RoutingDecision": {
                "type": "object",
                "required": ["layer", "reason", "priority"],
                "properties": {
                    "layer": {"type": "string"},
                    "reason": {"type": "string"},
                    "priority": {"type": "integer"},
                    "alternatives": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "layer": {"type": "string"},
                                "priority": {"type": "integer"},
                                "matched": {"type": "boolean"},
                            },
                        },
                    },
                    "features": {"type": "object"},
                },
            },
        },
    },
    "action": {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "Action",
        "type": "object",
        "required": ["type"],
        "properties": {
            "type": {
                "type": "string",
                "enum": [
                    "click",
                    "double_click",
                    "hover",
                    "type",
                    "fill",
                    "toggle",
                    "select",
                    "press_key",
                    "scroll",
                    "navigate",
                    "go_back",
                    "go_forward",
                    "right_click",
                    "launch_app",
                    "activate_app",
                    "wait",
                    "screenshot",
                    "webmcp_call",
                ],
            },
            "params": {
                "type": "object",
                "description": "Type-specific. click uses {ref} or {x,y}; fill uses {ref,text}. Optional verify: {kind: value|checked|selected|text, ref?: opaque ref, equals?: string|boolean, contains?: string, timeout_ms?: 0..10000}. Non-text conditions require ref. Invalid conditions prevent dispatch.",
            },
        },
    },
}
