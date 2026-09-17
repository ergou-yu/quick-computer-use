"""Repeatable local-browser QCU benchmark; no model latency or external accounts.

Run with the Python environment used by QCU. Uses an isolated QCU_HOME and
temporary Chromium profile; only a loopback fixture is operated. JSON records
include raw command results, wall time, payload bytes, and UI success checks.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


HTML = """<!doctype html><meta charset="utf-8"><title>QCU local benchmark</title>
<h1>QCU local benchmark</h1><form id="form">
<label>Name<input aria-label="Name"></label>
<label>Email<input aria-label="Email"></label>
<label>City<input aria-label="City"></label>
<label>Company<input aria-label="Company"></label>
<label>Role<input aria-label="Role"></label>
<label>Notes<input aria-label="Notes"></label>
<button>Save local fixture</button></form><h2 id="result">Pending</h2>
<script>
let saves=0;
form.onsubmit=e=>{e.preventDefault(); saves++;
 result.textContent='Saved '+saves+': '+Array.from(form.querySelectorAll('input'),e=>e.value).join(' | ')};
if(location.search.includes('busy'))setInterval(()=>fetch('/poll'),100);
</script>"""


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/poll'):
            time.sleep(.3)
            body = b'ok'
        else:
            body = HTML.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mode', choices=['separate', 'batch'], default='separate')
    p.add_argument('--rounds', type=int, default=5)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    records, runs = [], []
    server = ThreadingHTTPServer(('127.0.0.1', 0), Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='qcu-bench-') as home:
        env = dict(os.environ, QCU_HOME=home, QCU_HEADLESS='1')
        env.pop('QCU_DAEMON_PROCESS', None)
        env.pop('QCU_USER_DATA_DIR', None)

        def call(*words):
            start = time.perf_counter()
            proc = subprocess.run([sys.executable, '-m', 'qcu', *words], cwd=root,
                                  env=env, text=True, capture_output=True, timeout=60)
            elapsed = (time.perf_counter()-start)*1000
            payload = json.loads(proc.stdout) if proc.stdout.strip() else None
            records.append(dict(argv=words, wall_ms=elapsed, bytes=len(proc.stdout.encode()),
                                rc=proc.returncode, result=payload, stderr=proc.stderr))
            if proc.returncode:
                raise RuntimeError(f'{words[0]} failed: {proc.stdout} {proc.stderr}')
            return payload

        try:
            call('session', 'start', '--context', 'web')
            for busy in [False, True]:
                for n in range(args.rounds):
                    url = f'http://127.0.0.1:{server.server_port}/?{"busy" if busy else "static"}'
                    call('act', json.dumps(dict(type='navigate', params=dict(url=url))))
                    start_record = len(records)
                    obs = call('observe', '--compact')
                    controls = {e['name']: e['ref'] for e in obs['elements']}
                    values = dict(Name=f'Test {n}', Email='qcu@example.invalid', City='上海',
                                  Company='Local fixture', Role='Tester', Notes='AX / DOM test')
                    actions = [dict(type='fill', params=dict(ref=controls[k], text=v)) for k,v in values.items()]
                    expected = 'Saved 1: '+' | '.join(values.values())
                    actions.append(dict(type='click', params=dict(ref=controls['Save local fixture'],
                                       verify=dict(kind='text', equals=expected))))
                    if args.mode == 'batch':
                        call('batch', json.dumps(actions))
                    else:
                        for action in actions:
                            call('act', json.dumps(action))
                    final = call('observe')
                    expected = 'Saved 1: '+' | '.join(values.values())
                    assert expected in final.get('raw_tree', ''), final
                    field_values = {e['name']: e['value'] for e in final['elements'] if e['role']=='edit'}
                    assert field_values == values, field_values
                    sampled = records[start_record:]
                    runs.append(dict(busy=busy, round=n, success=True,
                                     wall_ms=sum(r['wall_ms'] for r in sampled),
                                     observe_ms=sum(r['wall_ms'] for r in sampled if r['argv'][0]=='observe'),
                                     calls=len(sampled), bytes=sum(r['bytes'] for r in sampled)))
        finally:
            # Stop Python before clearing its recorded pid, including old QCU versions.
            for command in [('daemon', 'stop'), ('session', 'end', '--purge-profile')]:
                try:
                    call(*command)
                except Exception:
                    pass
            server.shutdown()
            server.server_close()
            summary = {}
            for busy in [False, True]:
                selected = [r for r in runs if r['busy']==busy]
                if selected:
                    summary['busy' if busy else 'static'] = dict(
                        successes=len(selected),
                        median_wall_ms=statistics.median(r['wall_ms'] for r in selected),
                        median_observe_ms=statistics.median(r['observe_ms'] for r in selected),
                        median_bytes=statistics.median(r['bytes'] for r in selected),
                        calls=selected[0]['calls'])
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(dict(mode=args.mode, summary=summary, runs=runs,
                                                  records=records), ensure_ascii=False, indent=2))
            print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
