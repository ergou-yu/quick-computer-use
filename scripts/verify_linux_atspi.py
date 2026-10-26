"""Real GTK/AT-SPI/X11 or GNOME Wayland validation in a disposable session.

Creates only owned disposable windows and QCU_HOME. Does not install services,
change compositor settings, alter keymaps/clipboards or touch user apps.
"""
from __future__ import annotations
import argparse
import json
import importlib.metadata
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid


def verify(output):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Linux interactive DBus/AT-SPI/X11 session required")
    root=Path(__file__).resolve().parents[1]
    output=Path(output).absolute()
    output.parent.mkdir(parents=True,exist_ok=True)
    report=dict(platform=sys.platform,python=sys.version,qcu_version=importlib.metadata.version("qcu"),
                environment={k:os.environ.get(k) for k in ("XDG_SESSION_TYPE","DISPLAY","WAYLAND_DISPLAY","GDK_BACKEND")},
                ok=False,records=[])
    wayland=os.environ.get("XDG_SESSION_TYPE")=="wayland"
    if wayland:
        from gi.repository import Gio,GLib
        control=Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION,Gio.DBusProxyFlags.DO_NOT_AUTO_START,
            None,"org.gnome.Shell","/org/gnome/Shell/Extensions/QCUTest","org.qcu.Test1",None)
    with tempfile.TemporaryDirectory(prefix="qcu-linux-live-") as tmp:
        state=Path(tmp)
        env=dict(os.environ,QCU_HOME=str(state/"qcu"),PYTHONUTF8="1",NO_AT_BRIDGE="0",GTK_MODULES="gail:atk-bridge")
        env.pop("QCU_DAEMON_PROCESS",None)
        env.pop("QCU_USER_DATA_DIR",None)
        title="QCU Linux fixture "+uuid.uuid4().hex[:10]
        commands=state/"command.json"
        log_path=output.with_suffix(".fixture.log")
        log=log_path.open("w")
        fixture=subprocess.Popen([sys.executable,str(root/"examples/linux_fixture.py"),title,str(commands)],env=env,stdout=log,stderr=log)
        def call(*args,payload=None,expect=0,record=True):
            start=time.perf_counter()
            proc=subprocess.run([sys.executable,"-m","qcu",*args],cwd=root,env=env,
                                input=json.dumps(payload,ensure_ascii=False) if payload is not None else None,
                                capture_output=True,encoding="utf-8",timeout=40)
            try: result=json.loads(proc.stdout)
            except ValueError: raise AssertionError(proc.stdout+proc.stderr) from None
            if record: report["records"].append(dict(command=list(args),payload=payload,code=proc.returncode,
                wall_ms=(time.perf_counter()-start)*1000,result=result))
            if expect is not None: assert proc.returncode==expect,(args,result,proc.stderr)
            return result
        def act(kind,params,expect=0):
            return call("act","-",payload=dict(type=kind,params=params),expect=expect)
        def observed(): return call("observe","--compact")
        def refs(obs): return {(e.get("name") or e.get("value")):e["ref"] for e in obs["elements"] if e.get("name") or e.get("value")}
        def command(op,**data):
            if wayland and op=="overlay":
                # A GTK Wayland popup grabs focus. Use only the owned Shell
                # actor here so the test isolates pointer occlusion from focus.
                control.call_sync("Request",GLib.Variant("(s)",(json.dumps(dict(op=op,title=title,pid=fixture.pid,**data)),)),0,3000,None)
                time.sleep(.15)
                return
            staged=commands.with_suffix(".tmp")
            staged.write_text(json.dumps(dict(op=op,**data)))
            staged.replace(commands)
            deadline=time.monotonic()+5
            while commands.exists():
                assert time.monotonic()<deadline,"fixture command timed out"
                time.sleep(.03)
            if wayland:
                control.call_sync("Request",GLib.Variant("(s)",(json.dumps(dict(op=op,title=title,pid=fixture.pid,**data)),)),0,3000,None)
            # Setup-only settling for the owned WM fixture, never QCU action delays.
            time.sleep(.15)
        def evidence(text): return dict(kind="text",equals=text,timeout_ms=2000)
        try:
            call("session","start","--context","desktop")
            deadline=time.monotonic()+20
            while True:
                obs=call("observe","--pid",str(fixture.pid),"--window",title,"--compact",expect=None,record=False)
                if obs.get("routing_meta",{}).get("available"): break
                assert fixture.poll() is None,log_path.read_text()
                assert time.monotonic()<deadline,obs
                time.sleep(.15)
            report["records"].append(dict(command=["observe","--pid",str(fixture.pid)],result=obs))
            r=refs(obs)
            command("focus")
            assert act("fill",dict(ref=r["Name"],text="Linux 中文 🚀"))["outcome"]=="verified"
            assert act("toggle",dict(ref=r["Check"],verify=dict(kind="checked",ref=r["Check"],equals=True)))["outcome"]=="verified"
            assert act("select",dict(ref=r["Item"],verify=dict(kind="selected",ref=r["Item"],equals=True)))["outcome"]=="verified"
            batch=call("batch","-","--observe","--compact",payload=[
                dict(type="fill",params=dict(ref=r["Name"],text="Batch 中文")),
                dict(type="fill",params=dict(ref=r["City"],text="上海")),
                dict(type="click",params=dict(ref=r["Save"],verify=evidence("Saved 1")))])
            assert batch["ok"] and batch["executed"]==3,batch
            # batch --observe invalidates the preceding observation's refs.
            old=r["Save"]
            obs=observed()
            r=refs(obs)
            stale=act("click",dict(ref=old),expect=2)
            assert stale["dispatch_state"]=="not_sent",stale
            before=act("screenshot",dict(path=str(output.parent/"linux-before.png")))
            assert before["outcome"]=="verified"
            assert Path(before["data"]["path"]).read_bytes()[:8]==b"\x89PNG\r\n\x1a\n"
            command("focus")
            assert act("click",dict(ref=r["Save"],input_mode="native",verify=evidence("Saved 2")))["outcome"]=="verified"
            command("focus")
            assert act("press_key",dict(ref=r["Name"],key="F6",verify=evidence("Key F6")))["outcome"]=="verified"
            act("press_key",dict(ref=r["Name"],key="Ctrl+a"))
            act("press_key",dict(ref=r["Name"],key="End"))
            assert act("type",dict(ref=r["Name"],text=" 追加🚀",verify=dict(kind="value",ref=r["Name"],equals="Batch 中文 追加🚀")))["outcome"]=="verified"
            panel=next(e for e in obs["elements"] if e["name"]=="Pointer panel")
            if wayland:
                hover=act("hover",dict(ref=panel["ref"],verify=evidence("Hover")))
                x,y=hover["data"]["pointer_position"].values()
            else:
                b=panel["bounds"]
                x,y=b["x"]+b["width"]//2,b["y"]+b["height"]//2
            for kind,text,extra in (("hover","Hover",{}),("right_click","Right",{}),
                                    ("double_click","Double",{}),("scroll","Wheel",dict(dy=120)),
                                    ("drag","Dragged",dict(x2=x+40,y2=y+10))):
                if wayland and kind=="hover": continue
                result=act(kind,dict(x=x,y=y,verify=evidence(text),**extra))
                assert result["outcome"]=="verified",result
            fallback=call("observe","--layer","screenshot_fallback","--compact")
            assert fallback["routing_meta"]["captured"],fallback
            command("overlay",x=x,y=y)
            blocked=act("click",dict(x=x,y=y),expect=2)
            assert blocked["dispatch_state"]=="not_sent" and blocked["data"]["reason"]=="point_occluded",blocked
            command("clear")
            command("other")
            blocked=act("press_key",dict(key="F6"),expect=2)
            assert blocked["dispatch_state"]=="not_sent" and blocked["data"]["reason"] in {"foreground_mismatch","focus_mismatch"},blocked
            command("clear")
            if wayland and os.environ.get("QCU_TEST_MULTIMONITOR") == "1":
                command("move",x=420,y=250,monitor=1)
                report["multimonitor_move"] = True
            else:
                command("move",x=420,y=250)
            moved=act("screenshot",dict(path=str(output.parent/"linux-moved.png")))
            assert moved["data"]["coordinate_origin"]!=before["data"]["coordinate_origin"],moved
            if wayland:
                assert act("click",dict(ref=r["Save"],input_mode="native",verify=evidence("Saved 3")))["outcome"]=="verified"
                extensions=Gio.DBusProxy.new_for_bus_sync(Gio.BusType.SESSION,Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                    None,"org.gnome.Shell","/org/gnome/Shell","org.gnome.Shell.Extensions",None)
                extensions.call_sync("DisableExtension",GLib.Variant("(s)",("qcu@quick-computer-use",)),0,3000,None)
                blocked=act("press_key",dict(key="F6"),expect=2)
                assert blocked["dispatch_state"]=="not_sent" and blocked["data"]["reason"]=="wayland_bridge_unavailable",blocked
                assert act("fill",dict(ref=r["Name"],text="Semantic without bridge"))["outcome"]=="verified"
                extensions.call_sync("EnableExtension",GLib.Variant("(s)",("qcu@quick-computer-use",)),0,3000,None)
                blocked=act("press_key",dict(key="F6"),expect=2)
                assert blocked["dispatch_state"]=="not_sent" and blocked["data"]["reason"]=="target_changed",blocked
            assert act("fill",dict(ref=r["Name"],text="Semantic still fast"))["outcome"]=="verified"
            report.update(ok=True,cli_commands=len(report["records"]),target=obs["routing_meta"]["target"])
        except Exception as exc:
            report["error"]=str(exc)
            try: call("observe","--compact",expect=None)
            except Exception: pass
            raise
        finally:
            try: call("session","end",expect=None)
            finally:
                fixture.terminate()
                try: fixture.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    fixture.kill()
                    fixture.wait()
                log.close()
                report["cli_commands"]=len(report["records"])
                output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    return report


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output",default="linux-live.json")
    args=parser.parse_args()
    result=verify(args.output)
    print(json.dumps(dict(ok=result["ok"],cli_commands=result["cli_commands"],output=args.output)))
