"""Real CLI -> RPC -> Chromium tests against disposable loopback pages."""
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest


class Pages(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/verify':
            body = '''<title>QCU isolated verification fixture</title>
            <input aria-label="Value"><input type="checkbox" aria-label="Enabled">
            <button onclick="document.querySelector('h1').textContent='Saved '+(++window.count)">Save once</button>
            <h1>Ready</h1><script>window.count=0</script>'''
        elif self.path == '/delayed':
            body = '''<title>Async test</title><h1>Loading</h1><script>
            setTimeout(()=>document.body.insertAdjacentHTML('beforeend',
            '<button id="late">Ready target</button>'),350)</script>'''
        else:
            body = '''<title>Batch stop test</title>
            <label>First<input aria-label="First" oninput="document.querySelector('#second')?.remove()"></label>
            <label>Second<input id="second" aria-label="Second"></label>
            <button onclick="document.querySelector('h1').textContent='Submitted'">Submit test</button>
            <h1>Not submitted</h1>'''
        data = body.encode()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def live(tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QCU_HOME=str(tmp_path/'qcu'), QCU_HEADLESS='1')
    env.pop('QCU_DAEMON_PROCESS', None)
    env.pop('QCU_USER_DATA_DIR', None)
    server = ThreadingHTTPServer(('127.0.0.1',0),Pages)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def call(*args, expected=0):
        proc = subprocess.run([sys.executable,'-m','qcu',*args], cwd=root,env=env,
                              text=True,capture_output=True,timeout=30)
        assert proc.returncode == expected, proc.stdout+proc.stderr
        raw = proc.stdout or proc.stderr
        return json.loads(raw)
    call('session','start','--context','web')
    yield call, f'http://127.0.0.1:{server.server_port}', tmp_path/'qcu'/'session.json'
    call('session','end','--purge-profile')
    server.shutdown()
    thread.join()
    server.server_close()


@pytest.mark.integration
def test_delayed_target_wait_and_rpc_error_recovery(live):
    call, url, path = live
    call('act',json.dumps(dict(type='navigate',params=dict(url=url+'/delayed'))))
    obs = call('observe','--wait-for','#late','--compact')
    assert any(e['name']=='Ready target' for e in obs['elements'])
    error = call('observe','--wait-for','#missing','--timeout-ms','100', expected=1)
    assert 'Timeout' in error['error']
    assert call('daemon','status')['alive']
    assert call('observe','--compact')['elements']


@pytest.mark.integration
def test_dynamic_form_stops_before_submit_and_returns_fresh_state(live):
    call, url, path = live
    call('act',json.dumps(dict(type='navigate',params=dict(url=url+'/form'))))
    obs = call('observe','--compact')
    refs = {e['name']:e['ref'] for e in obs['elements']}
    result = call('batch',json.dumps([
        dict(type='fill',params=dict(ref=refs['First'],text='one')),
        dict(type='fill',params=dict(ref=refs['Second'],text='two')),
        dict(type='click',params=dict(ref=refs['Submit test']))]), '--observe', expected=2)
    assert result['executed']==2 and result['stopped_at']==1
    assert result['results'][1]['data']['dispatched'] is False
    assert 'Not submitted' in result['observation']['raw_tree']
    assert all(e['name']!='Second' for e in result['observation']['elements'])


@pytest.mark.integration
def test_session_end_stops_resident_process_and_invalid_refs(live):
    call, url, path = live
    call('act',json.dumps(dict(type='navigate',params=dict(url=url+'/form'))))
    obs = call('observe','--compact')
    ref = obs['elements'][0]['ref']
    call('observe','--compact')
    rejected = call('batch',json.dumps([dict(type='fill',params=dict(ref=ref,text='old'))]), expected=2)
    assert rejected['executed']==0
    pid = json.loads(path.read_text())['daemon_pid']
    call('session','end','--purge-profile')
    assert not path.exists()
    from qcu.layers.browser_daemon import is_pid_alive
    assert not is_pid_alive(pid)


@pytest.mark.integration
def test_verified_value_toggle_text_and_unknown_batch_do_not_replay(live):
    call, url, path = live
    call('act', json.dumps(dict(type='navigate', params=dict(url=url+'/verify'))))
    obs = call('observe', '--compact')
    assert obs['routing_meta']['target']['tab_id']
    refs = {e['name']:e['ref'] for e in obs['elements']}
    filled = call('act', json.dumps(dict(type='fill',params=dict(ref=refs['Value'],text='Disposable'))))
    assert filled['outcome'] == 'verified'
    toggled = call('act', json.dumps(dict(type='click',params=dict(ref=refs['Enabled'],verify={
        'kind':'checked','ref':refs['Enabled'],'equals':True}))))
    assert toggled['outcome'] == 'verified'
    clicked = call('act', json.dumps(dict(type='click',params=dict(ref=refs['Save once'],verify={
        'kind':'text','equals':'Saved 1'}))))
    assert clicked['outcome'] == 'verified' and clicked['dispatch_state'] == 'sent'
    unknown = call('batch', json.dumps([dict(type='click',params=dict(ref=refs['Save once']))]),
                   '--observe',expected=2)
    assert unknown['executed'] == 1 and unknown['reason'] == 'outcome_unknown'
    assert 'Saved 2' in unknown['observation']['raw_tree']
    assert 'Saved 3' not in unknown['observation']['raw_tree']


@pytest.mark.integration
def test_web_ref_rejected_after_backend_restart(live):
    call, url, path = live
    call('act', json.dumps(dict(type='navigate',params=dict(url=url+'/verify'))))
    obs = call('observe', '--compact')
    ref = next(e['ref'] for e in obs['elements'] if e['name']=='Save once')
    call('daemon', 'stop')
    result = call('act', json.dumps(dict(type='click',params=dict(ref=ref))),expected=2)
    assert result['dispatch_state'] == 'not_sent'
    assert 'Ready' in call('observe')['raw_tree']
