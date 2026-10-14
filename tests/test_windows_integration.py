"""Real Windows CDP bridge; disposable Chromium models an app's CDP endpoint."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from qcu.layers import browser_daemon as bd


@pytest.mark.integration
@pytest.mark.windows
@pytest.mark.skipif(sys.platform != "win32", reason="Windows PID/port discovery")
def test_windows_cdp_bridge_keeps_dom_route_and_external_process(tmp_path):
    from playwright.sync_api import sync_playwright

    fixture = tmp_path / "app.html"
    fixture.write_text('''<meta charset="utf-8"><title>QCU Windows CDP fixture</title>
        <input aria-label="Name"><input aria-label="City">
        <button onclick="document.querySelector('h1').textContent='Saved '+(++window.count)">Save</button>
        <h1>Ready</h1><script>window.count=0</script>''', encoding="utf-8")
    pid, port = bd.launch_and_wait(user_data_dir=str(tmp_path / "external-profile"), headless=True)
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QCU_HOME=str(tmp_path / "qcu-home"), PYTHONUTF8="1")
    env.pop("QCU_DAEMON_PROCESS", None)
    env.pop("QCU_USER_DATA_DIR", None)

    def call(*args, payload=None):
        proc = subprocess.run([sys.executable, "-m", "qcu", *args], cwd=root, env=env,
                              input=payload, capture_output=True, encoding="utf-8", timeout=30)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        return json.loads(proc.stdout)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(bd.cdp_url(port))
            browser.contexts[0].pages[0].goto(fixture.as_uri())
            # Stop the local driver only, leaving the external app alive.
        call("session", "start", "--context", "desktop")
        obs = call("observe", "--layer", "desktop_cdp", "--pid", str(pid),
                   "--window", "QCU Windows CDP fixture", "--compact")
        assert obs["routing_meta"]["bridged_via"] == "web_a11y"
        refs = {e["name"]: e["ref"] for e in obs["elements"]}
        filled = call("act", "-", payload=json.dumps({"type": "fill", "params": {
            "ref": refs["Name"], "text": "Windows 中文 🚀"}}, ensure_ascii=False))
        assert filled["layer"] == "desktop_cdp" and filled["outcome"] == "verified"
        batch = call("batch", "-", "--observe", "--compact", payload=json.dumps([
            {"type": "fill", "params": {"ref": refs["City"], "text": "上海"}},
            {"type": "click", "params": {"ref": refs["Save"],
                "verify": {"kind": "text", "equals": "Saved 1"}}}], ensure_ascii=False))
        assert batch["ok"] and batch["executed"] == 2
        assert all(r["layer"] == "desktop_cdp" for r in batch["results"])
        fresh = call("observe", "--compact")
        assert fresh["routing_meta"]["layer"] == "desktop_cdp"
        assert next(e["value"] for e in fresh["elements"] if e["name"] == "Name") == "Windows 中文 🚀"
        call("session", "end")
        assert bd.is_pid_alive(pid) and bd.is_cdp_alive(port)
    finally:
        try:
            call("session", "end")
        finally:
            bd.kill_pid(pid)
