#!/usr/bin/env python3
"""Real Windows UIA smoke test using only a disposable WinForms fixture.

Windows: py -m pip install -e ".[windows]"
         py scripts/verify_windows_uia.py

Runs no mocks. Non-Windows hosts exit 2 with an explicit unverified report.
It creates a separate QCU_HOME, starts its own local window, tests semantic
Patterns, and terminates only its own fixture process. No default browser or
business application is opened or touched.
"""
from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


FIXTURE = r'''
param([string]$ReadyPath, [string]$Title)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$form = New-Object System.Windows.Forms.Form
$form.Text = $Title
$form.Width = 520
$form.Height = 350
$form.StartPosition = 'CenterScreen'
$input = New-Object System.Windows.Forms.TextBox
$input.AccessibleName = 'QCU input'
$input.Location = New-Object System.Drawing.Point(20,20)
$input.Width = 300
$button = New-Object System.Windows.Forms.Button
$button.Text = 'QCU invoke'
$button.AccessibleName = 'QCU invoke'
$button.Location = New-Object System.Drawing.Point(20,60)
$button.Width = 130
$result = New-Object System.Windows.Forms.Label
$result.Text = 'QCU idle'
$result.Location = New-Object System.Drawing.Point(170,65)
$result.AutoSize = $true
$script:qcuInvocations = 0
$button.Add_Click({ $script:qcuInvocations++; $result.Text = 'QCU invoked ' + $script:qcuInvocations })
$toggle = New-Object System.Windows.Forms.CheckBox
$toggle.Text = 'QCU toggle'
$toggle.AccessibleName = 'QCU toggle'
$toggle.Location = New-Object System.Drawing.Point(20,100)
$toggle.Width = 180
$list = New-Object System.Windows.Forms.ListBox
$list.AccessibleName = 'QCU choices'
$list.Location = New-Object System.Drawing.Point(20,140)
$list.Width = 200
[void]$list.Items.Add('QCU first choice')
[void]$list.Items.Add('QCU second choice')
$disabled = New-Object System.Windows.Forms.Button
$disabled.Text = 'QCU disabled'
$disabled.AccessibleName = 'QCU disabled'
$disabled.Enabled = $false
$disabled.Location = New-Object System.Drawing.Point(250,140)
$disabled.Width = 160
$form.Controls.AddRange(@($input,$button,$result,$toggle,$list,$disabled))
$form.Add_Shown({
  @{pid=$PID; hwnd=$form.Handle.ToInt64(); title=$form.Text} |
    ConvertTo-Json -Compress | Set-Content -Encoding UTF8 $ReadyPath
})
[System.Windows.Forms.Application]::Run($form)
'''


def verify_cli(target: dict, workspace: Path) -> dict:
    """Separate CLI processes must share one live UIA worker and scoped refs."""
    env = dict(os.environ, QCU_HOME=str(workspace / "cli-home"), PYTHONUTF8="1")
    env.pop("QCU_DAEMON_PROCESS", None)
    env.pop("QCU_USER_DATA_DIR", None)
    records = []
    root = Path(__file__).resolve().parents[1]

    def call(*words, expected=0, payload=None):
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "qcu", *words], cwd=root, env=env,
                              input=payload, capture_output=True, encoding="utf-8", timeout=30)
        result = json.loads(proc.stdout or proc.stderr)
        records.append({"command": words, "wall_ms": (time.perf_counter()-start)*1000,
                        "exit_code": proc.returncode, "result": result})
        if proc.returncode != expected:
            raise AssertionError(f"CLI {words[0]} returned {proc.returncode}: {result}")
        return result

    def refs(obs):
        return {e["name"]: e["ref"] for e in obs["elements"]}

    def action(kind, params, expected=0):
        result = call("act", "-", expected=expected,
                      payload=json.dumps({"type": kind, "params": params}, ensure_ascii=False))
        if expected == 0:
            assert result["outcome"] == "verified", result
        return result

    try:
        call("session", "start", "--context", "desktop")
        first = call("observe", "--pid", str(target["pid"]), "--window", str(target["hwnd"]), "--compact")
        assert first["routing_meta"]["layer"] == "desktop_uia"
        warm = call("observe", "--compact")
        current = refs(warm)
        assert warm["routing_meta"]["target"]["window_id"] == target["hwnd"]
        action("fill", {"ref": current["QCU input"], "text": "Windows 中文 🚀"})
        action("click", {"ref": current["QCU invoke"],
                         "verify": {"kind": "text", "equals": "QCU invoked 2"}})
        action("toggle", {"ref": current["QCU toggle"],
                          "verify": {"kind": "checked", "ref": current["QCU toggle"], "equals": False}})
        action("select", {"ref": current["QCU second choice"]})
        disabled = action("click", {"ref": current["QCU disabled"]}, expected=2)
        assert disabled["dispatch_state"] == "not_sent" and disabled["data"]["reason"] == "control_disabled"
        batch = call("batch", "-", "--observe", "--compact", payload=json.dumps([
            {"type": "fill", "params": {"ref": current["QCU input"], "text": "批量 中文 🚀"}},
            {"type": "click", "params": {"ref": current["QCU invoke"],
                "verify": {"kind": "text", "equals": "QCU invoked 3"}}}], ensure_ascii=False))
        assert batch["ok"] and batch["executed"] == 2
        old = action("fill", {"ref": current["QCU input"], "text": "must not write"}, expected=2)
        assert old["dispatch_state"] == "not_sent" and old["data"]["reason"] == "stale_ref"
        current = refs(batch["observation"])
        unknown = call("batch", "-", "--observe", "--compact", expected=2, payload=json.dumps([
            {"type": "click", "params": {"ref": current["QCU invoke"]}}]))
        assert unknown["executed"] == 1 and unknown["reason"] == "outcome_unknown"
        names = [e["name"] for e in unknown["observation"]["elements"]]
        assert "QCU invoked 4" in names and "QCU invoked 5" not in names
        daemon = call("daemon", "status")
        assert daemon["alive"]
        old_ref = refs(unknown["observation"])["QCU input"]
        call("daemon", "stop")
        rejected = action("fill", {"ref": old_ref, "text": "must not write"}, expected=2)
        assert rejected["dispatch_state"] == "not_sent"
        call("observe", "--pid", str(target["pid"]), "--window", str(target["hwnd"]), "--compact")
        return {"ok": True, "transport": "CLI -> resident daemon -> real UIA", "records": records}
    finally:
        call("session", "end")
        assert not (workspace / "cli-home" / "session.json").exists()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="save UTF-8 JSON evidence")
    args = parser.parse_args()
    if sys.platform == "win32":
        sys.stdout.reconfigure(encoding="utf-8")

    def report(payload):
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(raw + "\n", encoding="utf-8")
        print(raw)

    if sys.platform != "win32":
        report({"ok": False, "status": "Windows real device unverified",
                "reason": "requires_windows_interactive_desktop"})
        return 2
    with tempfile.TemporaryDirectory(prefix="qcu-uia-validation-") as scratch:
        workspace = Path(scratch)
        os.environ["QCU_HOME"] = str(workspace / "qcu-home")
        from qcu import session
        from qcu.common.types import Action
        from qcu.layers.desktop_uia import DesktopUIALayer
        from qcu.platforms import desktop_backend_name
        session.new("desktop", desktop_backend_name())
        fixture_path = workspace / "fixture.ps1"
        fixture_path.write_text(FIXTURE, encoding="utf-8-sig")
        ready = workspace / "ready.json"
        child = subprocess.Popen(["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass",
                                  "-File", str(fixture_path), "-ReadyPath", str(ready),
                                  "-Title", "QCU UIA fixture " + uuid.uuid4().hex[:8]],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, encoding="utf-8",
                                 errors="replace", creationflags=subprocess.CREATE_NO_WINDOW)
        layer = DesktopUIALayer()
        checks = []
        observation = None
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and time.monotonic() < deadline and child.poll() is None:
                time.sleep(.05)
            if not ready.exists():
                raise RuntimeError("Disposable Windows fixture did not become ready: " +
                                   (child.stderr.read() if child.poll() is not None else "timeout"))
            target = json.loads(ready.read_text(encoding="utf-8-sig"))
            observation = layer.observe(pid=target["pid"], window=str(target["hwnd"]))
            if not observation.routing_meta.get("available"):
                raise RuntimeError("UIA target not readable: " + json.dumps(observation.routing_meta))
            def unique(name):
                found = [element for element in observation.elements if element.name == name]
                if len(found) != 1:
                    raise AssertionError(f"Expected one {name!r}, found {len(found)}; inspect UIA provider output")
                return found[0].ref
            def verified(action):
                start = time.perf_counter()
                result = layer.act(action)
                checks.append({"action": action.type, "wall_ms": (time.perf_counter()-start)*1000,
                               **result.to_dict()})
                if result.outcome != "verified":
                    raise AssertionError("Requested UI state was not verified: " + result.to_json())
            edit = unique("QCU input")
            verified(Action("fill", {"ref": edit, "text": "QCU disposable value"}))
            verified(Action("click", {"ref": unique("QCU invoke"),
                                      "verify": {"kind": "text", "equals": "QCU invoked 1"}}))
            toggle = unique("QCU toggle")
            verified(Action("toggle", {"ref": toggle,
                "verify": {"kind": "checked", "ref": toggle, "equals": True}}))
            verified(Action("select", {"ref": unique("QCU second choice")}))
            disabled = layer.act(Action("click", {"ref": unique("QCU disabled")}))
            assert disabled.dispatch_state == "not_sent" and disabled.data["reason"] == "control_disabled"
            checks.append({"check": "disabled_rejected", **disabled.to_dict()})
            layer.observe(pid=target["pid"], window=str(target["hwnd"]))
            stale = layer.act(Action("fill", {"ref": edit, "text": "must not be written"}))
            assert stale.dispatch_state == "not_sent" and stale.data["reason"] == "stale_ref"
            checks.append({"check": "stale_ref_rejected", **stale.to_dict()})
            layer.close()
            cli = verify_cli(target, workspace)
            report({"ok": True, "status": "Windows real UIA fixture verified",
                    "target": target, "checks": checks, "cli": cli})
            return 0
        except Exception as exc:
            report({"ok": False, "status": "Windows real UIA validation failed",
                    "error": str(exc), "checks": checks,
                    "observation": observation.to_dict() if observation else None})
            return 1
        finally:
            layer.close()
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=3)
            session.clear()


if __name__ == "__main__":
    raise SystemExit(main())
