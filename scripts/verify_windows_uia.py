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
$button.Add_Click({ $result.Text = 'QCU invoked' })
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


def main() -> int:
    if sys.platform != "win32":
        print(json.dumps({"ok": False, "status": "Windows real device unverified",
                          "reason": "requires_windows_interactive_desktop"}))
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
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
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
                result = layer.act(action)
                checks.append({"action": action.type, **result.to_dict()})
                if result.outcome != "verified":
                    raise AssertionError("Requested UI state was not verified: " + result.to_json())
            edit = unique("QCU input")
            verified(Action("fill", {"ref": edit, "text": "QCU disposable value"}))
            verified(Action("click", {"ref": unique("QCU invoke"),
                                      "verify": {"kind": "text", "equals": "QCU invoked"}}))
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
            print(json.dumps({"ok": True, "status": "Windows real UIA fixture verified",
                              "target": target, "checks": checks}, ensure_ascii=False, indent=2))
            return 0
        except Exception as exc:
            print(json.dumps({"ok": False, "status": "Windows real UIA validation failed",
                              "error": str(exc), "checks": checks,
                              "observation": observation.to_dict() if observation else None},
                             ensure_ascii=False, indent=2))
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
