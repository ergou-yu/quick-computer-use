"""Real GDI/SendInput smoke test on an isolated WinForms fixture; no mocks."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import uuid


FIXTURE = r'''
param([string]$ReadyPath,[string]$CommandPath,[string]$Title)
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
Add-Type -ReferencedAssemblies System.Windows.Forms,System.Drawing -TypeDefinition @'
using System;
using System.IO;
using System.Drawing;
using System.Windows.Forms;
using System.Runtime.InteropServices;
public class QCUOverlay : Form {
 protected override bool ShowWithoutActivation { get { return true; } }
 protected override CreateParams CreateParams { get { var p=base.CreateParams; p.ExStyle|=0x08000000; return p; } }
}
public class QCUSurface : Panel {
 public QCUSurface() { SetStyle(ControlStyles.Selectable,true); TabStop=true; }
}
public static class QCUKeyboardTrace {
 delegate IntPtr HookProc(int code,IntPtr kind,IntPtr packet);
 [StructLayout(LayoutKind.Sequential)] struct Packet { public uint vk,scan,flags,time; public UIntPtr extra; }
 [DllImport("user32.dll",SetLastError=true)] static extern IntPtr SetWindowsHookEx(int id,HookProc callback,IntPtr module,uint thread);
 [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook,int code,IntPtr kind,IntPtr packet);
 [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("kernel32.dll",CharSet=CharSet.Unicode)] static extern IntPtr GetModuleHandle(string name);
 static HookProc callback; static IntPtr hook,owned; static string path;
 public static void Start(IntPtr window,string output) {
  if(Environment.GetEnvironmentVariable("QCU_TRACE_INPUT")!="1") return;
  owned=window; path=output;
  callback=(code,kind,pointer)=>{ if(code>=0 && GetForegroundWindow()==owned) {
   Packet p=(Packet)Marshal.PtrToStructure(pointer,typeof(Packet));
   File.AppendAllText(path,"vk="+p.vk+" scan="+p.scan+" flags="+p.flags+" kind="+kind.ToInt64()+"\n");
  } return CallNextHookEx(hook,code,kind,pointer); };
  hook=SetWindowsHookEx(13,callback,GetModuleHandle(null),0);
  File.AppendAllText(path,"hook="+hook.ToInt64()+" error="+Marshal.GetLastWin32Error()+"\n");
 }
 public static void Stop() { if(hook!=IntPtr.Zero) UnhookWindowsHookEx(hook); }
}
public class QCUInputFilter : IMessageFilter {
 Label trace; public QCUInputFilter(Label label) { trace=label; }
 public bool PreFilterMessage(ref Message m) { if(m.Msg==0x100 || m.Msg==0x201 || m.Msg==0x202) trace.Text="Raw "+m.Msg.ToString("X")+" "+m.WParam.ToInt64(); return false; }
}
public class QCUNativeFixture : Form {
 [DllImport("user32.dll")] static extern bool SetForegroundWindow(IntPtr hwnd);
 [DllImport("user32.dll")] static extern IntPtr GetForegroundWindow();
 [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd,IntPtr pid);
 [DllImport("kernel32.dll")] static extern uint GetCurrentThreadId();
 [DllImport("user32.dll")] static extern bool AttachThreadInput(uint from,uint to,bool attach);
 void FixtureForeground() {
  // Test harness only: explicitly focus its own disposable window.
  uint current=GetCurrentThreadId(), other=GetWindowThreadProcessId(GetForegroundWindow(),IntPtr.Zero);
  bool attached=current!=other && AttachThreadInput(current,other,true);
  try { BringToFront(); Activate(); SetForegroundWindow(Handle); edit.Focus(); }
  finally { if(attached) AttachThreadInput(current,other,false); }
 }
 Label result=new Label(), keys=new Label(), wheel=new Label(), drag=new Label(), hover=new Label();
 TextBox edit=new TextBox(); QCUSurface surface=new QCUSurface(); int clicks=0, rights=0, doubles=0, wheels=0;
 bool dragging=false; Point start; Form overlay; string commands;
 public QCUNativeFixture(string title,string ready,string command) {
  Text=title; commands=command; Size=new Size(720,540); StartPosition=FormStartPosition.CenterScreen; TopMost=true;
  edit.AccessibleName="Native input"; edit.Location=new Point(20,20); edit.Width=420;
  Button button=new Button(); button.Text="Native click"; button.AccessibleName="Native click"; button.Bounds=new Rectangle(20,65,140,32);
  button.Click+=(s,e)=>{ result.Text="Clicked "+(++clicks); };
  result.Text="Idle"; result.Bounds=new Rectangle(180,70,250,28);
  keys.Text="Keys idle"; keys.Bounds=new Rectangle(20,110,400,25);
  edit.KeyDown+=(s,e)=>{ if(e.Control && e.KeyCode==Keys.A) keys.Text="Ctrl+A observed"; if(e.KeyCode==Keys.F6) keys.Text="F6 observed"; if(e.KeyCode==Keys.F7) keys.Text="F7 observed"; if(e.Control && e.KeyCode==Keys.F8) keys.Text="Ctrl+F8 observed"; };
  edit.PreviewKeyDown+=(s,e)=>{ if(e.KeyCode==Keys.F6 || e.KeyCode==Keys.F7 || e.KeyCode==Keys.F8) e.IsInputKey=true; };
  surface.AccessibleName="Native surface"; surface.BackColor=Color.FromArgb(30,140,230); surface.Bounds=new Rectangle(20,170,440,180);
  surface.MouseDown+=(s,e)=>{ surface.Focus(); if(e.Button==MouseButtons.Left) { dragging=true; start=e.Location; } if(e.Button==MouseButtons.Right) result.Text="Right "+(++rights); };
  surface.MouseUp+=(s,e)=>{ if(dragging && Math.Abs(e.X-start.X)>40) drag.Text="Drag done"; dragging=false; };
  surface.MouseDoubleClick+=(s,e)=>{ result.Text="Double "+(++doubles); };
  surface.MouseEnter+=(s,e)=>{ hover.Text="Hover observed"; };
  surface.MouseWheel+=(s,e)=>{ wheel.Text="Wheel "+(++wheels); };
  wheel.Text="Wheel idle"; wheel.Bounds=new Rectangle(20,370,250,25);
  drag.Text="Drag idle"; drag.Bounds=new Rectangle(20,400,250,25);
  hover.Text="Hover idle"; hover.Bounds=new Rectangle(20,430,250,25);
  Label trace=new Label(); trace.Text="Raw idle"; trace.Bounds=new Rectangle(300,430,360,25); Application.AddMessageFilter(new QCUInputFilter(trace));
  Panel marker=new Panel(); marker.BackColor=Color.Red; marker.Bounds=new Rectangle(530,170,100,100); marker.AccessibleName="Capture marker";
  Controls.AddRange(new Control[]{edit,button,result,keys,surface,wheel,drag,hover,marker,trace});
  Shown+=(s,e)=>{ FixtureForeground(); QCUKeyboardTrace.Start(Handle,ready+".keys"); File.WriteAllText(ready,"{\"pid\":"+System.Diagnostics.Process.GetCurrentProcess().Id+",\"hwnd\":"+Handle.ToInt64()+"}"); };
  FormClosed+=(s,e)=>{ QCUKeyboardTrace.Stop(); };
  Timer timer=new Timer(); timer.Interval=30;
  timer.Tick+=(s,e)=>{ if(!File.Exists(commands)) return; string cmd=File.ReadAllText(commands).Trim();
   if(cmd=="overlay") { overlay=new QCUOverlay(); overlay.FormBorderStyle=FormBorderStyle.None; overlay.ShowInTaskbar=false; overlay.TopMost=true; overlay.BackColor=Color.Magenta; overlay.StartPosition=FormStartPosition.Manual; overlay.Bounds=new Rectangle(PointToScreen(surface.Location),surface.Size); overlay.Show(this); SetForegroundWindow(Handle); edit.Focus(); }
   if(cmd=="clear") { if(overlay!=null) overlay.Close(); FixtureForeground(); }
   if(cmd=="move") { Location=new Point(Left+45,Top+20); }
   if(cmd=="clear_keys") { keys.Text="Keys idle"; FixtureForeground(); }
   if(cmd=="background") { overlay=new Form(); overlay.Text="QCU disposable other foreground"; overlay.Size=new Size(200,120); overlay.Show(); overlay.Activate(); SetForegroundWindow(overlay.Handle); }
   File.Delete(commands);
  }; timer.Start();
 }
 public static void Run(string title,string ready,string command) { Application.Run(new QCUNativeFixture(title,ready,command)); }
}
'@
[QCUNativeFixture]::Run($Title,$ReadyPath,$CommandPath)
'''


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--check-send-input',action='store_true',help='record optional physical injection result separately')
    parser.add_argument('--require-send-input',action='store_true',help='require verified physical keyboard, Unicode and pointer results')
    parser.add_argument('--trace-input',action='store_true',help='trace keyboard packet metadata only while the owned fixture is foreground')
    args=parser.parse_args()
    if sys.platform != 'win32':
        print(json.dumps(dict(ok=False,reason='requires_windows_interactive_desktop')))
        return 2
    sys.stdout.reconfigure(encoding='utf-8')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    artifacts=args.output.parent/'native-images'
    artifacts.mkdir(parents=True,exist_ok=True)
    records=[]
    report=dict(ok=False,platform='real Windows',version=None,records=records)
    root=Path(__file__).resolve().parents[1]
    interpreter=Path(sys.executable).with_name('pythonw.exe')
    if not interpreter.exists(): interpreter=Path(sys.executable)
    with tempfile.TemporaryDirectory(prefix='qcu-native-live-') as temp:
        scratch=Path(temp)
        env=dict(os.environ,QCU_HOME=str(scratch/'qcu-home'),PYTHONUTF8='1')
        if args.trace_input: env['QCU_TRACE_INPUT']='1'
        env.pop('QCU_DAEMON_PROCESS',None)
        env.pop('QCU_USER_DATA_DIR',None)
        script,ready,commands=scratch/'fixture.ps1',scratch/'ready.json',scratch/'commands.txt'
        script.write_text(FIXTURE,encoding='utf-8-sig')
        child=subprocess.Popen(['powershell.exe','-NoProfile','-STA','-ExecutionPolicy','Bypass',
            '-File',str(script),'-ReadyPath',str(ready),'-CommandPath',str(commands),'-Title','QCU Native '+uuid.uuid4().hex[:8]],
            stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,encoding='utf-8',errors='replace',env=env,creationflags=subprocess.CREATE_NO_WINDOW)

        def call(*words,expected=0,payload=None):
            start=time.perf_counter()
            p=subprocess.run([str(interpreter),'-m','qcu',*words],cwd=root,env=env,input=payload,
                encoding='utf-8',capture_output=True,timeout=30,creationflags=subprocess.CREATE_NO_WINDOW)
            data=json.loads(p.stdout or p.stderr)
            records.append(dict(command=words,wall_ms=(time.perf_counter()-start)*1000,exit_code=p.returncode,result=data))
            assert (p.returncode in (0,2) if expected is None else p.returncode==expected),(words,data,p.stderr)
            return data
        def act(kind,params,expected=0,layer=None):
            words=['act','-']+(['--layer',layer] if layer else [])
            return call(*words,expected=expected,payload=json.dumps(dict(type=kind,params=params),ensure_ascii=False))
        def condition(text): return dict(kind='text',equals=text,timeout_ms=2000)
        def point(element,fx=.5,fy=.5):
            b=element['bounds']; return dict(x=b['x']+b['width']*fx,y=b['y']+b['height']*fy)
        def observe():
            obs=call('observe','--compact')
            return {e['name']:e for e in obs['elements']}
        def fixture_command(command):
            pending=commands.with_suffix('.tmp')
            pending.write_text(command,encoding='ascii')
            os.replace(pending,commands)
            deadline=time.monotonic()+5
            while commands.exists() and time.monotonic()<deadline: time.sleep(.01)
            assert not commands.exists(),'fixture command timed out'

        try:
            deadline=time.monotonic()+20
            while not ready.exists() and child.poll() is None and time.monotonic()<deadline: time.sleep(.02)
            if not ready.exists(): raise RuntimeError(child.stderr.read() if child.poll() is not None else 'fixture readiness timeout')
            target=json.loads(ready.read_text())
            report['target']=target
            report['version']=subprocess.check_output([str(interpreter),'-m','qcu','--version'],cwd=root,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW).strip()
            call('session','start','--context','desktop')
            obs=call('observe','--pid',str(target['pid']),'--window',str(target['hwnd']),'--compact')
            elements={e['name']:e for e in obs['elements']}
            edit=elements['Native input']['ref']
            first=act('screenshot',dict(path=str(artifacts/'before.png')))
            assert first['outcome']=='verified' and first['data']['capture_method']=='Win32.PrintWindow/GDI'
            image=(artifacts/'before.png').read_bytes()
            assert image[:8]==b'\x89PNG\r\n\x1a\n'
            assert struct.unpack('>II',image[16:24])==(first['data']['width'],first['data']['height'])
            fixture_command('clear')
            assert act('press_key',dict(ref=edit,key='F6',verify=condition('F6 observed')))['outcome']=='verified'
            click=act('click',{**point(elements['Native click']),'verify':condition('Clicked 1')})
            assert click['outcome']=='verified' and click['data']['method']=='Win32.WindowMessage'
            # Input-mode ref uses live bounds, preserving the same UIA ref identity.
            act('click',dict(ref=edit,input_mode='native'))
            act('fill',dict(ref=edit,text='Replace this original'))
            keyed=act('press_key',dict(ref=edit,key='Ctrl+A'))
            assert keyed['outcome']=='verified'
            typed=act('type',dict(ref=edit,text='Windows 中文 🚀',verify=dict(kind='value',ref=edit,equals='Windows 中文 🚀',timeout_ms=2000)))
            assert typed['outcome']=='verified'
            assert act('press_key',dict(ref=edit,key='F6',verify=condition('F6 observed')))['outcome']=='verified'
            surface=elements['Native surface']
            assert act('hover',{**point(surface),'verify':condition('Hover observed')})['outcome']=='verified'
            assert act('right_click',{**point(surface),'verify':condition('Right 1')})['outcome']=='verified'
            assert act('double_click',{**point(surface),'verify':condition('Double 1')})['outcome']=='verified'
            # Move to a different point before dragging, avoiding a new double-click.
            start,end=point(surface,.15,.5),point(surface,.85,.5)
            assert act('drag',dict(x1=start['x'],y1=start['y'],x2=end['x'],y2=end['y'],verify=condition('Drag done')))['outcome']=='verified'
            assert act('scroll',{**point(surface),'dy':120,'verify':condition('Wheel 1')})['outcome']=='verified'
            second=act('screenshot',dict(path=str(artifacts/'after.png')),layer='screenshot_fallback')
            assert second['outcome']=='verified' and second['data']['target']['window_id']==target['hwnd']
            # Pixel fallback explicitly delegates to the same bound HWND.
            assert act('click',{**point(elements['Native click']),'verify':condition('Clicked 2')},layer='screenshot_fallback')['outcome']=='verified'
            fixture_command('overlay')
            refused=act('click',point(surface),expected=2)
            assert refused['dispatch_state']=='not_sent' and refused['data']['reason']=='target_occluded'
            fixture_command('clear')
            fixture_command('background')
            refused=act('type',dict(text='must not write'),expected=2)
            assert refused['dispatch_state']=='not_sent' and refused['data']['reason']=='foreground_mismatch'
            fixture_command('clear')
            fixture_command('move')
            moved=act('screenshot',dict(path=str(artifacts/'moved.png')))
            assert moved['data']['coordinate_origin'] != first['data']['coordinate_origin']
            fresh=observe()
            assert fresh['Native input']['value']=='Windows 中文 🚀'
            stale=act('click',dict(ref=edit,input_mode='native'),expected=2)
            assert stale['dispatch_state']=='not_sent' and stale['data']['reason']=='stale_ref'
            # Original fast semantic route remains available after native operations.
            semantic=act('fill',dict(ref=fresh['Native input']['ref'],text='UIA fast route retained'))
            assert semantic['outcome']=='verified' and semantic['data']['method']=='UIA.value'
            if args.check_send_input or args.require_send_input:
                fixture_command('clear_keys')
                probe_key='F7'
                physical=act('press_key',dict(ref=fresh['Native input']['ref'],key=probe_key,input_transport='send_input',verify=condition(probe_key+' observed')),expected=None)
                report['send_input_probe']=dict(verified=physical.get('outcome')=='verified',result=physical)
                if args.require_send_input:
                    assert physical.get('outcome')=='verified',physical
                    # Distinct requested actions, each with independent result readback.
                    combo=act('press_key',dict(ref=fresh['Native input']['ref'],key='Ctrl+F8',input_transport='send_input',verify=condition('Ctrl+F8 observed')))
                    assert combo['outcome']=='verified',combo
                    # Prepare an empty field semantically. A received Ctrl+A is
                    # not proof that this provider implements Select All.
                    assert act('fill',dict(ref=fresh['Native input']['ref'],text=''))['outcome']=='verified'
                    typed=act('type',dict(ref=fresh['Native input']['ref'],text='Physical 中文 🚀',input_transport='send_input',verify=dict(kind='value',ref=fresh['Native input']['ref'],equals='Physical 中文 🚀',timeout_ms=2000)))
                    assert typed['outcome']=='verified',typed
                    clicked=act('click',{**point(fresh['Native click']),'input_transport':'send_input','verify':condition('Clicked 3')})
                    assert clicked['outcome']=='verified',clicked
                    report['send_input_required']=dict(verified=True,checks=[probe_key,'Ctrl+F8','Unicode value','pointer click'])
                observe()  # Read after dispatch; never repeat an unconfirmed input.
            report['ok']=True
        except Exception as exc:
            report['error']=str(exc)
            foreground_pid=records[-1].get('result',{}).get('data',{}).get('foreground_pid') if records else None
            if foreground_pid:
                diagnostic=subprocess.run(['powershell.exe','-NoProfile','-Command',
                    'Get-Process -Id '+str(int(foreground_pid))+' | Select-Object Id,ProcessName,MainWindowTitle | ConvertTo-Json -Compress'],
                    capture_output=True,encoding='utf-8',errors='replace',creationflags=subprocess.CREATE_NO_WINDOW)
                report['foreground_diagnostic']=diagnostic.stdout
            try:
                report['failure_observation']=call('observe','--compact')
                act('screenshot',dict(path=str(artifacts/'failure.png')))
            except Exception: pass
            if (scratch/'qcu-home'/'session.json').exists():
                report['failure_session']=json.loads((scratch/'qcu-home'/'session.json').read_text(encoding='utf-8'))
        finally:
            try: call('session','end')
            except Exception as exc: report['cleanup_error']=str(exc)
            if child.poll() is None:
                child.terminate()
                try: child.wait(timeout=3)
                except subprocess.TimeoutExpired: child.kill(); child.wait(timeout=3)
            report['fixture_stopped']=child.poll() is not None
            trace=Path(str(ready)+'.keys')
            if trace.exists(): report['physical_key_trace']=trace.read_text(encoding='utf-8')
            args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(dict(ok=report['ok'],error=report.get('error'),commands=len(records),output=str(args.output)),ensure_ascii=False))
    return 0 if report['ok'] else 1


if __name__=='__main__': raise SystemExit(main())
