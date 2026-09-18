"""Desktop JS bridge — drive a WKWebView app through its embedded debug bridge.

WKWebView content is not externally automatable: AX exposes only the
AXWebArea shell, and Apple's remote-inspection path (Safari Develop menu)
requires private entitlements plus manual GUI steps — verified on macOS 26
(no webinspectord, no programmatic client). For apps the owner controls,
embedding ``examples/QCUWebViewBridge.swift`` exposes a loopback-only,
token-authenticated evaluate endpoint; this layer discovers it and maps
observe/act to DOM semantics — full controls, no screenshots.

Discovery trusts only a bridge file that (a) names the *bound* pid, (b) comes
from the bridge directory (``$QCU_BRIDGE_DIR`` or ``~/.qcu/bridges``), and
(c) answers ``/qcu/info`` with the file's token on the advertised port. A
missing or stale bridge is reported, never substituted with another target.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Optional
from urllib.request import Request, urlopen

from qcu.common.refs import NativeRefRegistry
from qcu.common.types import Action, Element, LayerResult, Observation
from qcu.layers.base import Layer
from qcu.layers.runtime import register

# Extracts interactive elements, tags each with data-qcu-ref="qcu_<i>" and
# returns a JSON string. Re-tagging every observe keeps tags and refs in sync
# even if the app re-renders between observations.
_OBSERVE_JS = r"""
(() => {
  document.querySelectorAll('[data-qcu-ref]').forEach(e => e.removeAttribute('data-qcu-ref'));
  const sel = 'a[href],button,input,textarea,select,summary,[contenteditable="true"],'
    + '[role="button"],[role="link"],[role="checkbox"],[role="textbox"],[role="switch"],'
    + '[role="tab"],[role="menuitem"],[role="option"],[onclick]';
  const out = [];
  let i = 0;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const tag = el.tagName.toLowerCase();
    const roleAttr = (el.getAttribute('role') || '').toLowerCase();
    let role = roleAttr || ({a:'link',button:'button',select:'combobox',textarea:'text_area',summary:'button'})[tag] || 'generic';
    if (tag === 'input') {
      const t = (el.type || 'text').toLowerCase();
      role = ({checkbox:'checkbox',radio:'radio',button:'button',submit:'button',range:'slider'})[t] || 'text_field';
    }
    const name = (el.getAttribute('aria-label') || (el.innerText || '').trim().slice(0, 120)
      || el.placeholder || el.title || '').trim() || null;
    const ref = 'qcu_' + i;
    el.setAttribute('data-qcu-ref', ref);
    out.push({i, role, name,
      value: (typeof el.value === 'string') ? el.value.slice(0, 500) : null,
      checked: (typeof el.checked === 'boolean') ? el.checked : null,
      disabled: !!el.disabled,
      cx: Math.round(r.x + r.width / 2), cy: Math.round(r.y + r.height / 2)});
    i++;
  }
  return JSON.stringify({title: document.title, url: location.href, elements: out});
})()
"""


def _bridge_dir() -> Path:
    env = os.environ.get("QCU_BRIDGE_DIR")
    if env:
        return Path(env)
    return Path.home() / ".qcu" / "bridges"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _http(method: str, url: str, *, token: Optional[str] = None,
          payload: Optional[dict[str, Any]] = None, timeout: float = 5.0) -> dict[str, Any]:
    body = json.dumps(payload).encode() if payload is not None else None
    req = Request(url, data=body, method=method,
                  headers={"Content-Type": "application/json",
                           **({"X-QCU-Token": token} if token else {})})
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def find_bridge(pid: int, *, opener=_http) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Locate the app's embedded bridge. Returns ``(bridge, diagnostics)``."""
    diag: dict[str, Any] = {"pid": pid}
    if not _pid_alive(pid):
        diag["reason"] = "pid_not_running"
        return None, diag
    file = _bridge_dir() / f"{pid}.json"
    if not file.is_file():
        diag["reason"] = "no_bridge_file"
        diag["hint"] = ("embed examples/QCUWebViewBridge.swift in the app and call "
                        "QCUWebViewBridge.shared.attach(webView); WKWebView content is "
                        "not externally automatable without it")
        return None, diag
    try:
        meta = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        diag["reason"] = "bridge_file_unreadable"
        diag["error"] = str(exc)
        return None, diag
    if meta.get("pid") != pid or not isinstance(meta.get("port"), int) or not meta.get("token"):
        diag["reason"] = "bridge_file_invalid"
        return None, diag
    url = f"http://127.0.0.1:{meta['port']}"
    try:
        info = opener("GET", f"{url}/qcu/info")
    except Exception as exc:  # noqa: BLE001 — refused/timeout: stale file
        diag["reason"] = "bridge_not_responding"
        diag["error"] = f"{type(exc).__name__}: {exc}"
        return None, diag
    if not info.get("ok"):
        diag["reason"] = "bridge_not_responding"
        return None, diag
    return {"url": url, "token": meta["token"], "app": meta.get("app", ""),
            "pid": pid, **{k: info[k] for k in ("webview_attached",) if k in info}}, diag


@register("desktop_jsbridge")
class DesktopJSBridgeLayer(Layer):
    name = "desktop_jsbridge"

    def __init__(self) -> None:
        self._refs = NativeRefRegistry(self.name)
        self._bound: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Target resolution (mirrors desktop_cdp: explicit pid, or unique app match)
    # ------------------------------------------------------------------

    def _resolve_pid(self, app: Optional[str], pid: Optional[int]) -> tuple[Optional[int], Optional[str]]:
        if pid is not None:
            try:
                pid = int(pid)
                return (pid, None) if pid > 0 else (None, "invalid_target")
            except (TypeError, ValueError):
                return None, "invalid_target"
        if app:
            try:
                from AppKit import NSWorkspace  # type: ignore
            except Exception:  # noqa: BLE001
                return None, "missing_capability"
            wanted = app.casefold()
            matches = [r for r in NSWorkspace.sharedWorkspace().runningApplications()
                       if wanted in str(r.localizedName() or "").casefold()
                       or wanted in str(r.bundleIdentifier() or "").casefold()]
            if len(matches) != 1:
                return None, "ambiguous_app" if matches else "app_not_found"
            return int(matches[0].processIdentifier()), None
        return None, "invalid_target"

    def _evaluate(self, expression: str) -> dict[str, Any]:
        return _http("POST", f"{self._bound['url']}/qcu/evaluate",
                     token=self._bound["token"], payload={"expression": expression})

    # ------------------------------------------------------------------
    # Layer interface
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        pid = options.get("pid")
        app = options.get("app")
        if pid is None and app is None and self._bound:
            pid = self._bound.get("pid")
        pid, err = self._resolve_pid(app, pid)
        if err is not None:
            return Observation(context="desktop", elements=[], routing_meta={
                "layer": self.name, "error": "desktop_jsbridge needs an explicit live --pid or unique --app.",
                "reason": err})

        bridge, diag = find_bridge(pid)
        if bridge is None:
            return Observation(context="desktop", url_or_app=str(app or pid), elements=[],
                               routing_meta={"layer": self.name, "error": "embedded JS bridge not available",
                                             **diag})

        self._bound = {"pid": pid, "url": bridge["url"], "token": bridge["token"],
                       "app": bridge["app"]}
        try:
            resp = self._evaluate(_OBSERVE_JS)
        except Exception as exc:  # noqa: BLE001
            return Observation(context="desktop", url_or_app=bridge["app"], elements=[],
                               routing_meta={"layer": self.name, "error": f"evaluate failed: {exc}",
                                             "reason": "evaluate_failed"})
        if not resp.get("ok") or not isinstance(resp.get("value"), str):
            return Observation(context="desktop", url_or_app=bridge["app"], elements=[],
                               routing_meta={"layer": self.name, "error": resp.get("error", "observe JS failed"),
                                             "reason": "evaluate_failed"})
        try:
            data = json.loads(resp["value"])
        except ValueError:
            data = {"title": None, "url": None, "elements": []}

        scope = self._refs.begin({"pid": pid, "bridge": bridge["url"]})
        elements: list[Element] = []
        refs_for_session: list[dict[str, Any]] = []
        for item in data.get("elements") or []:
            ref = self._refs.issue(int(item["i"]))
            elements.append(Element(
                ref=ref, role=str(item["role"]), name=item.get("name"),
                value=item.get("value"), enabled=not item.get("disabled", False),
                properties={"checked": item.get("checked"),
                            "cx": item.get("cx"), "cy": item.get("cy"),
                            "js_ref": f"qcu_{item['i']}"},
            ))
            refs_for_session.append({"ref": ref, "role": item["role"], "name": item.get("name")})

        try:
            from qcu.session import record_observation
            record_observation(
                {"context": "desktop", "url_or_app": bridge["app"], "title": data.get("title"),
                 "elements": [r["ref"] for r in refs_for_session],
                 "n_interactive": len(elements),
                 "routing_meta": {"layer": self.name, "jsbridge": {"pid": pid, "url": bridge["url"]}}},
                refs=refs_for_session)
        except Exception:  # noqa: BLE001
            pass

        return Observation(
            context="desktop", url_or_app=bridge["app"], title=data.get("title"),
            elements=elements,
            routing_meta={"layer": self.name, "target": {"pid": pid, "app": bridge["app"]},
                          "ref_scope": scope, "jsbridge": {"url": bridge["url"]},
                          "n_refs": len(elements), "n_interactive": len(elements)},
        )

    def _resolve_js_ref(self, ref: Optional[str]) -> tuple[Optional[str], Optional[LayerResult]]:
        if not ref:
            return None, LayerResult(False, self.name, "action needs params.ref; observe first",
                                     data={"reason": "invalid_target"}, dispatch_state="not_sent")
        ok, why = self._refs.validate(ref, {"pid": self._bound.get("pid"), "bridge": self._bound.get("url")})
        if not ok:
            return None, LayerResult(False, self.name, f"reference not current ({why}); observe again",
                                     data={"reason": why}, dispatch_state="not_sent")
        index = ref.rsplit("ref_", 1)[-1]
        return f"qcu_{index}", None

    def act(self, action: Action) -> LayerResult:
        if not self._bound:
            return LayerResult(False, self.name, "No JS bridge target bound; observe the app first.",
                               data={"reason": "target_unbound"}, dispatch_state="not_sent")
        if not _pid_alive(int(self._bound["pid"])):
            return LayerResult(False, self.name, "The bridged app exited; observe a live target.",
                               data={"reason": "target_unavailable"}, dispatch_state="not_sent")

        condition = action.params.get("verify")
        if condition is not None:
            from qcu.common.verification import validate_condition
            try:
                validate_condition(condition)
            except ValueError as exc:
                return LayerResult(False, self.name, str(exc), data={"reason": "invalid_verification"})

        js_ref, failure = self._resolve_js_ref(action.params.get("ref"))
        if failure is not None:
            return failure

        atype = action.type
        if atype == "click":
            js = self._click_js(js_ref)
        elif atype == "fill":
            js = self._fill_js(js_ref, str(action.params.get("value", "")))
        elif atype == "hover":
            js = self._hover_js(js_ref)
        else:
            return LayerResult(False, self.name,
                               f"desktop_jsbridge does not implement {atype!r}",
                               data={"reason": "unsupported"}, dispatch_state="not_sent")

        try:
            resp = self._evaluate(js)
        except Exception as exc:  # noqa: BLE001
            # The evaluate call itself failed; the action may or may not have
            # reached the page — never replay blindly.
            return LayerResult(False, self.name, f"dispatch outcome unknown: {exc}",
                               data={"reason": "outcome_unknown", "retry_safe": False},
                               dispatch_state="unknown", outcome="unknown")
        value = resp.get("value") if isinstance(resp.get("value"), dict) else {}
        status = value.get("status")
        if not resp.get("ok") or status == "dispatch_error":
            return LayerResult(False, self.name,
                               f"dispatch outcome unknown: {value.get('message') or resp.get('error')}",
                               data={"reason": "outcome_unknown", "retry_safe": False},
                               dispatch_state="unknown", outcome="unknown")
        if status == "not_found":
            return LayerResult(False, self.name, "element no longer in the DOM; observe again",
                               data={"reason": "stale_ref"}, dispatch_state="not_sent")
        result = LayerResult(True, self.name, f"{atype} dispatched",
                             data={"jsbridge": {"url": self._bound["url"], "pid": self._bound["pid"]}},
                             dispatch_state="sent")

        if condition is not None:
            evidence = self._verify(condition)
            result.data["verification"] = evidence
            result.outcome = "verified" if evidence["verified"] else "unknown"
            result.ok = bool(evidence["verified"])
            if not result.ok:
                result.data["reason"] = "outcome_unknown"
        return result

    def _verify(self, condition: dict[str, Any]) -> dict[str, Any]:
        from qcu.common.verification import match_condition
        deadline = time.monotonic() + condition.get("timeout_ms", 1000) / 1000
        while True:
            try:
                ref = condition.get("ref")
                if ref is not None:
                    js_ref, failure = self._resolve_js_ref(ref)
                    if failure is not None:
                        return {"verified": False, "reason": "verification_ref_missing_or_ambiguous"}
                    js = ("(() => {const el=document.querySelector('[data-qcu-ref=\"%s\"]');"
                          "if(!el)return null;"
                          "return {value:(typeof el.value==='string')?el.value:null,"
                          "name:(el.innerText||el.textContent||'').slice(0,500),"
                          "checked:(typeof el.checked==='boolean')?el.checked:null,"
                          "selected:(typeof el.selected==='boolean')?el.selected:null};})()") % js_ref
                    value = self._evaluate(js).get("value")
                    if not isinstance(value, dict):
                        return {"verified": False, "reason": "verification_ref_missing_or_ambiguous"}
                    elements = [Element(ref or "", "unknown", value=value.get("value"),
                                        name=value.get("name"),
                                        properties={"checked": value.get("checked"),
                                                    "selected": value.get("selected")})]
                else:
                    resp = self._evaluate("document.body ? document.body.innerText.slice(0, 5000) : ''")
                    elements = [Element("", "document", value=str(resp.get("value") or ""))]
                return match_condition(condition, elements)
            except Exception:  # noqa: BLE001
                if time.monotonic() >= deadline:
                    return {"verified": False, "reason": "condition_not_observed"}
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # Action JS snippets. status: ok | not_found | dispatch_error.
    # ------------------------------------------------------------------

    @staticmethod
    def _click_js(js_ref: Optional[str]) -> str:
        return ("(() => {const el=document.querySelector('[data-qcu-ref=\"%s\"]');"
                "if(!el)return {status:'not_found'};"
                "try{el.click();return {status:'ok'};}catch(e){return {status:'dispatch_error',message:String(e)};}"
                "})()") % js_ref

    @staticmethod
    def _fill_js(js_ref: Optional[str], text: str) -> str:
        safe = json.dumps(text)
        return ("(() => {const el=document.querySelector('[data-qcu-ref=\"%s\"]');"
                "if(!el)return {status:'not_found'};"
                "try{if(el.isContentEditable){el.textContent=%s;}else{el.value=%s;}"
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "el.dispatchEvent(new Event('change',{bubbles:true}));"
                "return {status:'ok'};}catch(e){return {status:'dispatch_error',message:String(e)};}"
                "})()") % (js_ref, safe, safe)

    @staticmethod
    def _hover_js(js_ref: Optional[str]) -> str:
        return ("(() => {const el=document.querySelector('[data-qcu-ref=\"%s\"]');"
                "if(!el)return {status:'not_found'};"
                "try{el.dispatchEvent(new MouseEvent('mouseover',{bubbles:true}));return {status:'ok'};}"
                "catch(e){return {status:'dispatch_error',message:String(e)};}"
                "})()") % js_ref

    def capabilities(self) -> dict[str, Any]:
        return {
            "backend": "desktop_jsbridge",
            "implemented": True,
            "requires": "target app embeds examples/QCUWebViewBridge.swift (owner-controlled apps only)",
            "bound": bool(self._bound),
            "reads_controls": True,
            "actions": ["click", "fill", "hover"],
            "verifies_results": True,
            "needs_foreground_input": False,
        }

    def close(self) -> None:
        self._bound = {}
        self._refs.invalidate()
