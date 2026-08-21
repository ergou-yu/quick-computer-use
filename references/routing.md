# Routing — how the classifier picks a layer

The MVP Router is a **priority chain of rules**. Each rule evaluates a
flat feature dict and either matches (returns True) or doesn't. The
highest-priority match wins. Every match result (winner + all
alternatives) is recorded so we can later train a learned classifier from
the same data.

## Rule table

Source of truth: `qcu/router/rules.py`. The current rules:

| priority | name           | layer                | condition (informal)                                             |
|---------:|----------------|----------------------|------------------------------------------------------------------|
| 100      | canvas_target  | screenshot_fallback  | a11y tree has `<canvas>` and target ref not in a11y              |
| 95       | visual_confirm | screenshot_fallback  | action.type in `submit_payment`, `delete_account`, `confirm_purchase` |
| 80       | webmcp_tool    | webmcp               | `navigator.modelContext` exposed tools AND action calls one       |
| 70       | web_a11y       | web_a11y             | context=web AND (ref in a11y OR no action yet)                    |
| 65       | desktop_ax     | desktop_ax           | context=desktop AND `ax_trusted`                                  |
| 55       | desktop_no_ax  | screenshot_fallback  | context=desktop (and AX not granted)                              |
| 40       | web_no_target  | screenshot_fallback  | context=web AND action has ref AND ref not in a11y AND no webmcp |
| 10       | default        | screenshot_fallback  | always matches                                                   |

## Adding a rule

Edit `qcu/router/rules.py` and append an entry. The tuple shape is:

```python
(priority: int, name: str, condition: Callable[[dict], bool],
 layer: str, reason: str)
```

Add a unit test in `tests/test_router.py`. Run `pytest tests/` to verify.

## Feature schema

The classifier only sees a JSON-friendly dict built by
`qcu/router/features.py:extract(observation, last_action)`. Keys:

```jsonc
{
  "context": "web" | "desktop",
  "url_or_app": "https://..." | "TextEdit" | null,
  "n_elements": int,                 // total elements in observation
  "n_interactive": int,              // elements with an interactive normalized role
  "roles": { "button": 3, "link": 7, ... },   // histogram
  "has_canvas": bool,
  "has_webmcp": bool,
  "webmcp_tool_count": int,
  "webmcp_tool_names": ["submit_order", ...],
  "has_screenshot": bool,            // observation already has a screenshot_path
  "has_raw_tree": bool,

  // Only populated when an Action is provided:
  "action_type": "click" | "fill" | ...,
  "action_params": {...},
  "action_ref": "ref_42" | null,
  "target_in_a11y": bool,
  "action_in_webmcp_tools": bool,
  "action_in_critical_list": bool,
  "needs_visual_confirm": bool,
  "has_canvas_target": bool,

  // Desktop-only, set by cli_handlers based on the live trust probe:
  "ax_trusted": bool
}
```

## Decision log

Every `observe()` and `act()` appends one line to
`~/.qcu/telemetry.jsonl` (override with `$QCU_HOME`). Each line is:

```jsonc
{
  "ts": 1735689600.123,
  "context": "web",
  "features": { ... },              // full feature dict
  "decision": {
    "layer": "web_a11y",
    "reason": "...",
    "priority": 70,
    "rule_name": "web_a11y",
    "alternatives": [...],
    "features": { ... }             // duplicated for self-contained records
  },
  "action": null | {"type":..., "params":...},
  "outcome": null | "ok" | "fail" | "error",
  "latency_ms": 23.4
}
```

Summarize with `qcu stats`. Aggregated by layer and outcome, with
p50/p95/avg latency:

```bash
qcu stats --since 7d
```

## Upgrading to a learned classifier

The Router's call site (`classify()` in `classifier.py`) is intentionally
minimal so you can swap implementations without touching the rest of the
code. To replace the rule chain with, e.g., a logistic regression trained
on `telemetry.jsonl`:

1. Collect labelled data by running with `QCU_HOME=/some/path` and
   occasionally overriding `--layer` to force a non-default choice.
2. Train on `(features, chosen_layer)` pairs.
3. Implement `classify_ml(features)` next to `classify()` and route based
   on a feature flag.

The rule engine stays useful as a fallback and a sanity check.