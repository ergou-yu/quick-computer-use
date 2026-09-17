#!/usr/bin/env python3
"""Disposable Cocoa fixture + real AX regression. Never touches business apps.

Run with QCU's macOS Python: python scripts/verify_macos_ax.py --output result.json
Returns 2 when permission/environment blocks the live test.
"""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def fixture(ready: str) -> None:
    import AppKit as A
    from Foundation import NSObject
    app = A.NSApplication.sharedApplication()
    app.setActivationPolicy_(A.NSApplicationActivationPolicyRegular)
    class Handler(NSObject):
        def clicked_(self, sender):
            self.count += 1
            self.result.setStringValue_(f'Saved {self.count}')
    handler = Handler.alloc().init()
    handler.count = 0
    windows = []
    for title in ['QCU isolated AX fixture', 'QCU isolated AX secondary']:
        window = A.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            A.NSMakeRect(150, 200, 440, 240), A.NSWindowStyleMaskTitled | A.NSWindowStyleMaskClosable,
            A.NSBackingStoreBuffered, False)
        window.setTitle_(title)
        windows.append(window)
    window = windows[0]
    entry = A.NSTextField.alloc().initWithFrame_(A.NSMakeRect(20, 170, 360, 28))
    entry.setAccessibilityLabel_('Fixture value')
    window.contentView().addSubview_(entry)
    button = A.NSButton.alloc().initWithFrame_(A.NSMakeRect(20, 115, 180, 32))
    button.setTitle_('Save once')
    button.setTarget_(handler)
    button.setAction_('clicked:')
    window.contentView().addSubview_(button)
    result = A.NSTextField.labelWithString_('Ready')
    result.setFrame_(A.NSMakeRect(20, 65, 360, 28))
    window.contentView().addSubview_(result)
    handler.result = result
    window.makeKeyAndOrderFront_(None)
    windows[1].orderFront_(None)
    window.makeKeyAndOrderFront_(None)
    Path(ready).write_text(str(os.getpid()))
    app.run()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--fixture')
    parser.add_argument('--output', default='macos-ax-result.json')
    args = parser.parse_args()
    if args.fixture:
        fixture(args.fixture)
        return 0
    if sys.platform != 'darwin':
        print('macOS required', file=sys.stderr)
        return 2
    records = []
    capture_status = 'not_run'
    with tempfile.TemporaryDirectory(prefix='qcu-ax-validation-') as directory:
        root = Path(directory)
        ready = root / 'ready'
        env = dict(os.environ, QCU_HOME=str(root/'qcu-home'))
        env.pop('QCU_USER_DATA_DIR', None)
        env.pop('QCU_DAEMON_PROCESS', None)
        child = subprocess.Popen([sys.executable, __file__, '--fixture', str(ready)], env=env,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        def call(*words):
            run = subprocess.run([sys.executable, '-m', 'qcu', *words], env=env,
                                 capture_output=True, text=True, timeout=40)
            raw = run.stdout or run.stderr
            payload = json.loads(raw)
            records.append(dict(argv=words, returncode=run.returncode, result=payload,
                                stderr=run.stderr))
            return payload
        status = 'blocked'
        try:
            deadline = time.monotonic()+10
            while not ready.exists() and child.poll() is None and time.monotonic()<deadline:
                time.sleep(.1)
            if not ready.exists():
                raise RuntimeError('Cocoa fixture failed to open')
            call('session', 'start', '--context', 'desktop')
            missing = call('observe', '--pid', str(child.pid), '--window', 'QCU missing window', '--compact')
            assert missing['routing_meta'].get('reason') == 'window_not_found', missing
            ambiguous = call('observe', '--pid', str(child.pid), '--window', 'QCU isolated AX', '--compact')
            assert ambiguous['routing_meta'].get('reason') == 'ambiguous_window', ambiguous
            obs = call('observe', '--pid', str(child.pid), '--window', 'QCU isolated AX fixture', '--compact')
            controls = {el['name']:el['ref'] for el in obs['elements'] if el.get('name')}
            if not controls:
                raise RuntimeError('No controls: '+str(obs.get('routing_meta')))
            filled = call('act', json.dumps({'type':'fill','params':{'ref':controls['Fixture value'],'text':'Disposable'}}))
            assert filled['outcome']=='verified', filled
            clicked = call('act', json.dumps({'type':'click','params':{'ref':controls['Save once'],
                          'verify':{'kind':'text','equals':'Saved 1'}}}))
            assert clicked['outcome']=='verified' and clicked['dispatch_state']=='sent', clicked
            capture_path = Path(args.output).resolve().with_suffix('.png')
            capture = call('act', json.dumps({'type':'screenshot','params':{'path':str(capture_path)}}))
            capture_status = 'passed' if capture.get('ok') and capture_path.is_file() else 'blocked'
            fresh = call('observe', '--compact')
            assert fresh['routing_meta']['target']['pid']==child.pid
            assert "text 'Saved 1'" in fresh.get('raw_tree', ''), fresh
            stale = call('act', json.dumps({'type':'click','params':{'ref':controls['Save once']}}))
            assert stale['dispatch_state']=='not_sent', stale
            status = 'passed'
        except Exception as exc:
            records.append({'error': str(exc)})
        finally:
            try:
                call('session','end','--purge-profile')
            except Exception as exc:
                records.append({'cleanup_error':str(exc)})
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
    Path(args.output).write_text(json.dumps({'status':status,'capture_status':capture_status,'records':records}, ensure_ascii=False,indent=2))
    print(json.dumps({'status':status,'output':str(Path(args.output).resolve())}))
    return 0 if status=='passed' else 2

if __name__=='__main__':
    raise SystemExit(main())
