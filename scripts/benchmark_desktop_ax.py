"""Desktop AX benchmark: macOS Calculator observe + real AXPress clicks.

Uses an independent QCU_HOME, binds the Calculator window by pid+title, runs
7+3=10 rounds with a final text verification, then quits Calculator and stops
the task daemon. Requires macOS Accessibility permission for this Python.
Reports timings, element counts and outcome states only.

    python scripts/benchmark_desktop_ax.py --output desktop-ax.json
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    records = []
    home = tempfile.mkdtemp(prefix="qcu-bench-dt-")
    env = dict(os.environ, QCU_HOME=home)
    env.pop("QCU_DAEMON_PROCESS", None)
    env.pop("QCU_USER_DATA_DIR", None)

    def call(*words, timeout=60):
        start = time.perf_counter()
        proc = subprocess.run([sys.executable, "-m", "qcu", *words], cwd=ROOT,
                              env=env, text=True, capture_output=True, timeout=timeout)
        elapsed = (time.perf_counter() - start) * 1000
        payload = json.loads(proc.stdout) if proc.stdout.strip() else None
        records.append(dict(argv=list(words), wall_ms=elapsed, rc=proc.returncode,
                            result=payload, stderr=proc.stderr))
        if proc.returncode:
            raise RuntimeError(f"{words} failed rc={proc.returncode}: "
                               f"{proc.stdout[:800]} {proc.stderr[:800]}")
        return payload

    subprocess.run(["open", "-a", "Calculator"], check=False)
    pid = None
    for _ in range(40):
        r = subprocess.run(["pgrep", "-x", "Calculator"], capture_output=True, text=True)
        if r.returncode == 0:
            pid = r.stdout.strip().split("\n")[0]
            break
        time.sleep(0.25)
    if not pid:
        raise SystemExit("Calculator did not start")

    runs, obs_stats, click_stats = [], [], []
    try:
        call("session", "start", "--context", "desktop")
        for n in range(args.rounds):
            t0 = len(records)
            obs = call("observe", "--pid", pid, "--window", "Calculator", "--compact")
            elements = obs.get("elements", [])

            def find(role, name):
                return next((e["ref"] for e in elements
                             if e.get("role") == role and e.get("name") == name), None)

            # English-locale AX names; digits are locale-stable.
            btn = {d: find("button", name=d) for d in ["7", "Add", "3", "Equals"]}
            if not all(btn.values()):
                raise RuntimeError(f"missing refs: {btn} n_elements={len(elements)}")
            call("act", json.dumps(dict(type="click", params=dict(ref=btn["7"]))))
            call("act", json.dumps(dict(type="click", params=dict(ref=btn["Add"]))))
            call("act", json.dumps(dict(type="click", params=dict(ref=btn["3"]))))
            call("act", json.dumps(dict(type="click", params=dict(
                ref=btn["Equals"], verify=dict(kind="text", contains="10", timeout_ms=1500)))))
            final = records[-1]["result"]
            runs.append(dict(round=n, elements=len(elements),
                             wall_ms=sum(r["wall_ms"] for r in records[t0:]),
                             last_outcome=(final or {}).get("outcome"),
                             last_dispatch=(final or {}).get("dispatch_state")))
        obs_stats = [r["wall_ms"] for r in records if r["argv"][0] == "observe"]
        click_stats = [r["wall_ms"] for r in records if r["argv"][0] == "act"
                       and json.loads(r["argv"][1])["type"] == "click"]
    finally:
        try:
            call("session", "end")
        except Exception:
            pass
        subprocess.run(["osascript", "-e", 'tell application "Calculator" to quit'],
                       capture_output=True)
        summary = dict(
            rounds=args.rounds,
            observe_p50_ms=statistics.median(obs_stats) if obs_stats else None,
            observe_n=len(obs_stats),
            click_p50_ms=statistics.median(click_stats) if click_stats else None,
            click_n=len(click_stats),
            elements=runs[0]["elements"] if runs else None,
            verified_final=[r["last_outcome"] for r in runs],
            runs=runs)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(dict(summary=summary, records=records),
                                          ensure_ascii=False, indent=2))
        print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
