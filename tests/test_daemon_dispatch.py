from concurrent.futures import ThreadPoolExecutor
import json
import socket
import threading
import time

import pytest

from qcu.daemon import server


@pytest.fixture
def rpc_server():
    svc = server._LoopbackTCPServer(('127.0.0.1', 0), server._Handler)
    thread = threading.Thread(target=svc.serve_forever, daemon=True)
    thread.start()
    yield svc.server_address[1]
    svc.shutdown()
    thread.join()
    svc.server_close()


def test_overlapping_commands_share_one_ui_thread(monkeypatch, rpc_server):
    ids, intervals = [], []
    def dispatch(method, params):
        ids.append(threading.get_ident())
        start = time.perf_counter()
        time.sleep(.025)
        intervals.append((start, time.perf_counter()))
        return dict(ok=True,result=params)
    monkeypatch.setattr(server, '_dispatch', dispatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(server.call_daemon, rpc_server, 'act', dict(index=i)) for i in range(4)]
        results = [f.result() for f in futures]
    assert len(set(ids)) == 1
    assert all(a[1] <= b[0] for a,b in zip(intervals,intervals[1:]))
    assert [r['result']['index'] for r in results] == list(range(4))


def test_ping_works_while_ui_is_busy(monkeypatch, rpc_server):
    entered, release = threading.Event(), threading.Event()
    def dispatch(*args):
        entered.set()
        release.wait(2)
        return dict(ok=True)
    monkeypatch.setattr(server, '_dispatch', dispatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(server.call_daemon, rpc_server, 'act', {})
        assert entered.wait(1)
        try:
            assert server.health_check(rpc_server, timeout=.5)
        finally:
            release.set()
        assert pending.result()['ok']


@pytest.mark.parametrize('raw', [[], {'method':'act','params':[]}, {'method':5}])
def test_malformed_rpc_returns_error(rpc_server, raw):
    with socket.create_connection(('127.0.0.1',rpc_server)) as connection:
        connection.sendall((json.dumps(raw)+'\n').encode())
        assert json.loads(connection.recv(4096))['ok'] is False


def test_lost_reply_never_replays_action_locally(monkeypatch, capsys):
    from qcu import cli_handlers as h
    monkeypatch.delenv('QCU_DAEMON_PROCESS', raising=False)
    monkeypatch.setattr(h, '_daemon_for_this_session', lambda: 9000)
    def lost(*args, **kwargs):
        raise server.DaemonError('lost reply', dispatched=True)
    monkeypatch.setattr(server, 'call_daemon', lost)
    assert h._maybe_route_via_daemon('batch', {}) == 2
    assert json.loads(capsys.readouterr().err)['retry_safe'] is False


def test_daemon_keeps_error_payload_and_diagnostics(monkeypatch):
    from qcu import cli_handlers as h
    import sys
    def act(*args, **kwargs):
        print(json.dumps(dict(ok=False, error='bad input')), file=sys.stderr)
        return 1
    monkeypatch.setattr(h, 'act', act)
    result = server._dispatch('act', {})
    assert result['exit_code'] == 1
    assert 'bad input' in result['stderr']
