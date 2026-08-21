"""Tests for embedded web-view (AXWebArea / WKWebView / Electron) detection.

The field report flagged: "Mirroria 的 WKWebView 没有向 AX 暴露内部 DOM" and
"QCU 网页层还缺少 Chromium 驱动". The Chromium daemon already exists; what
was missing was *detection*. These tests verify the detection signal flows
end-to-end:

  1. An observation whose routing_meta carries ``web_view`` produces
     ``features.has_web_area = True``.
  2. The ``desktop_webview_blind`` rule fires (priority 62) when the app is
     an opaque web-view shell (has_web_area AND n_interactive < 8).
  3. A real AppKit-style desktop app with an AXWebArea *plus* plenty of
     actable controls does NOT trigger the blind rule (the AX path still
     wins — only "essentially-a-web-view" shells escalate).
  4. The feature is also true when an element exposes raw_role="AXWebArea"
     directly (defensive path for layers that forget routing_meta).
"""

from __future__ import annotations

from qcu.common.types import Element, Observation
from qcu.router.classifier import classify
from qcu.router.features import extract


def _webview_shell_obs(*, n_interactive=2):
    """An observation like Mirroria: AXWebArea detected + few actable controls."""
    elements = [Element(ref=f"ref_{i}", role="button", name=f"chrome-{i}")
                for i in range(n_interactive)]
    return Observation(
        context="desktop",
        url_or_app="Mirroria",
        title="Mirroria",
        elements=elements,
        routing_meta={
            "layer": "desktop_ax",
            "trusted": True,
            "n_interactive": n_interactive,
            "web_view": {
                "app": "Mirroria",
                "url": None,
                "reason": "AXWebArea detected: opaque web view.",
            },
        },
    )


def _rich_desktop_with_webview(*, n_interactive=30):
    """A real AppKit app that *also* embeds a web view somewhere — has plenty
    of native controls, so it should NOT be flagged as blind."""
    elements = [Element(ref=f"ref_{i}", role="button", name=f"btn-{i}")
                for i in range(n_interactive)]
    return Observation(
        context="desktop",
        url_or_app="SomeRealApp",
        title="SomeRealApp",
        elements=elements,
        routing_meta={
            "layer": "desktop_ax",
            "trusted": True,
            "n_interactive": n_interactive,
            "web_view": {"app": "SomeRealApp", "url": None, "reason": "x"},
        },
    )


# ===========================================================================
# Feature extraction
# ===========================================================================


def test_has_web_area_true_when_routing_meta_carries_web_view():
    feats = extract(_webview_shell_obs())
    assert feats["has_web_area"] is True


def test_has_web_area_false_for_normal_desktop_app():
    # A plain desktop obs with no web_view meta and no AXWebArea element.
    obs = Observation(
        context="desktop",
        url_or_app="TextEdit",
        elements=[Element(ref="ref_1", role="button", name="OK")],
        routing_meta={},
    )
    assert extract(obs)["has_web_area"] is False


def test_has_web_area_true_via_element_raw_role():
    # Defensive path: a layer that forgot to set routing_meta["web_view"]
    # but did surface the AXWebArea as an element with its raw_role.
    obs = Observation(
        context="desktop",
        url_or_app="X",
        elements=[Element(ref="ref_0", role="document",
                          properties={"raw_role": "AXWebArea"})],
        routing_meta={},
    )
    assert extract(obs)["has_web_area"] is True


# ===========================================================================
# Routing rule desktop_webview_blind (priority 62)
# ===========================================================================


def test_webview_shell_triggers_desktop_webview_blind_rule():
    """The actual escalation case: Mirroria-like opaque shell. The blind
    rule wins over the plain desktop_ax rule and surfaces the takeover
    hint in the decision reason."""
    feats = extract(_webview_shell_obs(n_interactive=2))
    # Force ax_trusted True so desktop_ax (65) is otherwise a candidate.
    feats["ax_trusted"] = True
    d = classify(feats)
    assert d.rule_name == "desktop_webview_blind"
    assert d.priority == 66
    assert d.layer == "desktop_ax"  # keeps the desktop backend for chrome clicks
    assert "AXWebArea" in d.reason or "web" in d.reason.lower()


def test_rich_desktop_app_with_webview_does_not_trigger_blind_rule():
    """A real app that merely *embeds* a web view (e.g. a settings pane)
    still has plenty of native controls → stays on the normal desktop_ax
    path. The blind rule's n_interactive<8 guard prevents false escalation."""
    feats = extract(_rich_desktop_with_webview(n_interactive=30))
    feats["ax_trusted"] = True
    d = classify(feats)
    assert d.rule_name == "desktop_ax"  # not desktop_webview_blind
    assert d.priority == 65
    # The has_web_area feature is still true (informational) but the rule
    # didn't fire because the tree isn't sparse.
    assert feats["has_web_area"] is True


def test_webview_blind_alternative_appears_for_rich_app():
    """Even when the blind rule doesn't WIN, it should appear in the
    alternatives list (matched=False) so the LLM can see the option
    exists. This keeps the escalation path discoverable."""
    feats = extract(_rich_desktop_with_webview(n_interactive=30))
    feats["ax_trusted"] = True
    d = classify(feats)
    rule_names = [a["rule"] for a in d.alternatives]
    assert "desktop_webview_blind" in rule_names


# ===========================================================================
# Full browser via web_app (the CRM-in-Chrome field report)
#
# Stock Chrome does NOT expose AXWebArea without AXManualAccessibility, so
# the AXWebArea path above never fires for it — yet its DOM is just as
# opaque. The router must fire desktop_webview_blind off ``has_web_app``
# alone (populated by the AppleScript tab probe), even when the chrome
# toolbar yields dozens of actable controls (n_interactive >= 8).
# ===========================================================================


def _browser_obs(*, n_interactive=50):
    """Chrome with the CRM tab open: web_app populated, no AXWebArea,
    plenty of toolbar controls (the case the old rule missed)."""
    elements = [Element(ref=f"ref_{i}", role="button", name=f"chrome-{i}")
                for i in range(n_interactive)]
    return Observation(
        context="desktop",
        url_or_app="Google Chrome",
        title="CRM - Google Chrome",
        elements=elements,
        routing_meta={
            "layer": "desktop_ax",
            "trusted": True,
            "n_interactive": n_interactive,
            "web_app": {
                "browser": "Google Chrome",
                "tabs": [{"index": 1, "url": "https://crm.example.com", "title": "CRM"}],
            },
        },
    )


def test_has_web_app_true_for_browser():
    feats = extract(_browser_obs())
    assert feats["has_web_app"] is True
    # has_web_area stays False for stock Chrome (no AXWebArea exposed) —
    # the two signals are independent by design.
    assert feats["has_web_area"] is False


def test_has_web_app_false_for_normal_desktop_app():
    obs = Observation(
        context="desktop",
        url_or_app="TextEdit",
        elements=[Element(ref="ref_1", role="button", name="OK")],
        routing_meta={},
    )
    assert extract(obs)["has_web_app"] is False


def test_browser_triggers_desktop_webview_blind_rule():
    """The field-report fix: Chrome (n_interactive=50, way above the <8
    guard) still escalates to the blind rule because web_app is set."""
    feats = extract(_browser_obs(n_interactive=50))
    feats["ax_trusted"] = True
    d = classify(feats)
    assert d.rule_name == "desktop_webview_blind"
    assert d.priority == 66
    assert d.layer == "desktop_ax"
    assert "web" in d.reason.lower() or "browser" in d.reason.lower()


def test_non_browser_desktop_app_stays_on_desktop_ax():
    """A non-browser desktop app (no web_app, no AXWebArea) must NOT be
    flagged — it stays on the normal desktop_ax path. Regression guard
    against the new has_web_app branch over-firing."""
    obs = Observation(
        context="desktop",
        url_or_app="TextEdit",
        elements=[Element(ref="ref_1", role="text_area", name="body")],
        routing_meta={"layer": "desktop_ax", "trusted": True, "n_interactive": 1},
    )
    feats = extract(obs)
    feats["ax_trusted"] = True
    d = classify(feats)
    assert d.rule_name == "desktop_ax"
    assert d.priority == 65
