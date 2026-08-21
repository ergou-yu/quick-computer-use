"""Demo: feed the router a couple of synthetic features and print decisions.

Run from the package root:
    python3 examples/demo_router.py
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

# Allow running this script directly from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qcu.common.types import Action, Observation, Element  # noqa: E402
from qcu.router.classifier import classify  # noqa: E402
from qcu.router.features import extract  # noqa: E402


def banner(text: str) -> None:
    print()
    print("=" * 78)
    print(text)
    print("=" * 78)


def case_web_click_ref() -> None:
    banner("Web context — click ref_42 in a11y tree")
    obs = Observation(
        context="web",
        url_or_app="https://example.com/login",
        title="Login",
        elements=[
            Element(ref="ref_1", role="edit", name="Username"),
            Element(ref="ref_2", role="edit", name="Password"),
            Element(ref="ref_42", role="button", name="Sign in"),
        ],
    )
    feats = extract(obs, last_action=Action(type="click", params={"ref": "ref_42"}))
    d = classify(feats, last_action=Action(type="click", params={"ref": "ref_42"}))
    print(f"layer chosen: {d.layer}  (rule: {d.rule_name}, priority {d.priority})")
    print(f"reason: {d.reason}")


def case_canvas_target() -> None:
    banner("Web context — click on a canvas target")
    obs = Observation(
        context="web",
        url_or_app="https://example.com/game",
        elements=[
            Element(ref="ref_1", role="canvas", name="Game board"),
            Element(ref="ref_2", role="button", name="Restart"),
        ],
    )
    action = Action(type="click", params={"ref": "ref_canvas_42"})  # unknown ref
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    print(f"layer chosen: {d.layer}  (rule: {d.rule_name}, priority {d.priority})")
    print(f"reason: {d.reason}")


def case_critical_action() -> None:
    banner("Web context — submit_payment")
    obs = Observation(
        context="web",
        url_or_app="https://shop.example.com/checkout",
        elements=[Element(ref="ref_99", role="button", name="Pay now")],
    )
    action = Action(type="submit_payment", params={"ref": "ref_99"})
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    print(f"layer chosen: {d.layer}  (rule: {d.rule_name}, priority {d.priority})")
    print(f"reason: {d.reason}")


def case_webmcp_available() -> None:
    banner("Web context — page registered a WebMCP tool")
    obs = Observation(
        context="web",
        url_or_app="https://shop.example.com",
        elements=[Element(ref="ref_1", role="button", name="Filter")],
        tools=[{"name": "filter_products", "description": "Filter the product grid"}],
    )
    action = Action(type="webmcp_call", params={"tool": "filter_products", "args": {}})
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    print(f"layer chosen: {d.layer}  (rule: {d.rule_name}, priority {d.priority})")
    print(f"reason: {d.reason}")


def case_desktop_no_ax() -> None:
    banner("Desktop context — AX not trusted")
    obs = Observation(context="desktop", url_or_app="TextEdit", elements=[])
    feats = extract(obs)
    feats["ax_trusted"] = False  # simulate denial
    d = classify(feats)
    print(f"layer chosen: {d.layer}  (rule: {d.rule_name}, priority {d.priority})")
    print(f"reason: {d.reason}")


def main() -> None:
    case_web_click_ref()
    case_canvas_target()
    case_critical_action()
    case_webmcp_available()
    case_desktop_no_ax()
    print()
    print("(Each case prints the router's pick + the reason. Telemetry rows")
    print(" would normally be appended to ~/.qcu/telemetry.jsonl; this demo")
    print(" runs in-process and skips the file write.)")


if __name__ == "__main__":
    main()