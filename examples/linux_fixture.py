"""Disposable GTK3 fixture for scripts/verify_linux_atspi.py only."""
import json
from pathlib import Path
import sys
import cairo

import gi
gi.require_version("Gtk","3.0")
from gi.repository import Gtk, Gdk, GLib

title, command_path = sys.argv[1], Path(sys.argv[2])
window = Gtk.Window(title=title)
window.set_default_size(560,400)
window.move(80,80)
window.connect("destroy",Gtk.main_quit)
box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL,spacing=8)
box.set_border_width(14)
window.add(box)
entries = []
for name in ("Name","City"):
    entry=Gtk.Entry()
    entry.get_accessible().set_name(name)
    entries.append(entry)
    box.pack_start(entry,False,False,0)
saved, status = Gtk.Label(label="Ready"), Gtk.Label(label="Pointer ready")
count = 0
def save(*_):
    global count
    count+=1
    saved.set_text("Saved "+str(count))
button=Gtk.Button(label="Save")
button.connect("clicked",save)
box.pack_start(button,False,False,0)
check=Gtk.CheckButton(label="Check")
box.pack_start(check,False,False,0)
store=Gtk.ListStore(str)
store.append(["Item"])
items=Gtk.IconView.new()
items.set_model(store)
items.set_text_column(0)
items.set_selection_mode(Gtk.SelectionMode.SINGLE)
box.pack_start(items,False,False,0)
panel=Gtk.EventBox()
panel.get_accessible().set_name("Pointer panel")
panel.set_size_request(500,90)
canvas=Gtk.DrawingArea()
panel.add(canvas)
icon_count=0
duplicate_icon=False
def icon_image():
    image=cairo.ImageSurface(cairo.FORMAT_ARGB32,40,40)
    ctx=cairo.Context(image)
    ctx.set_source_rgb(.02,.05,.3); ctx.paint()
    ctx.set_source_rgb(1,.8,0); ctx.arc(17,17,14,0,6.2831853); ctx.fill()
    ctx.set_source_rgb(1,1,1); ctx.rectangle(20,22,16,12); ctx.fill()
    ctx.set_source_rgb(1,0,0); ctx.set_line_width(2); ctx.move_to(4,34); ctx.line_to(35,5); ctx.stroke()
    return image
def paint_icon(_,ctx):
    for x in ([300,360] if duplicate_icon else [300]):
        ctx.set_source_surface(icon_image(),x,20); ctx.paint()
canvas.connect("draw",paint_icon)
panel.add_events(Gdk.EventMask.BUTTON_PRESS_MASK|Gdk.EventMask.BUTTON_RELEASE_MASK|
                 Gdk.EventMask.POINTER_MOTION_MASK|Gdk.EventMask.ENTER_NOTIFY_MASK|
                 Gdk.EventMask.SCROLL_MASK)
press=None
def pointer(_,event):
    global press, icon_count
    if event.type==Gdk.EventType.ENTER_NOTIFY:
        status.set_text("Hover")
    elif event.type==Gdk.EventType.SCROLL:
        status.set_text("Wheel")
    elif event.type==Gdk.EventType._2BUTTON_PRESS:
        status.set_text("Double")
    elif event.type==Gdk.EventType.BUTTON_PRESS:
        press=(event.x_root,event.y_root)
        if event.button==3:
            status.set_text("Right")
        elif event.button==1 and 300<=event.x<340 and 20<=event.y<60:
            icon_count+=1
            status.set_text("Icon clicked "+str(icon_count))
    elif event.type==Gdk.EventType.BUTTON_RELEASE and press:
        if abs(event.x_root-press[0])+abs(event.y_root-press[1])>15:
            status.set_text("Dragged")
        press=None
    return False
for event in ("enter-notify-event","scroll-event","button-press-event","button-release-event"):
    panel.connect(event,pointer)
box.pack_start(panel,True,True,0)
box.pack_start(saved,False,False,0)
box.pack_start(status,False,False,0)
def key(_,event):
    if event.keyval==Gdk.KEY_F6:
        status.set_text("Key F6")
    return False
window.connect("key-press-event",key)
aux=[]
def command():
    global duplicate_icon
    if command_path.exists():
        data=json.loads(command_path.read_text())
        op=data["op"]
        if op=="template":
            duplicate_icon=data.get("duplicate",False)
            icon_image().write_to_png(data["path"])
            canvas.queue_draw()
        elif op=="move": window.move(data["x"],data["y"])
        elif op=="focus":
            window.present()
            entries[0].grab_focus()
            entries[0].select_region(0,0)
        elif op=="overlay":
            overlay=Gtk.Window(type=Gtk.WindowType.POPUP)
            overlay.set_accept_focus(False)
            overlay.set_focus_on_map(False)
            overlay.set_keep_above(True)
            overlay.set_default_size(100,100)
            overlay.move(data["x"]-30,data["y"]-30)
            overlay.add(Gtk.Label(label="Owned overlay"))
            overlay.show_all()
            aux.append(overlay)
        elif op=="other":
            other=Gtk.Window(title="QCU owned other window")
            other.add(Gtk.Entry())
            other.show_all()
            other.present()
            aux.append(other)
        elif op=="clear":
            for other in aux: other.destroy()
            aux.clear()
            window.present()
        command_path.unlink()
    return True
GLib.timeout_add(30,command)
window.show_all()
window.present()
entries[0].grab_focus()
Gtk.main()
