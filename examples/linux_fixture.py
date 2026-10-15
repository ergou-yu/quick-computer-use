"""Disposable GTK3 fixture for scripts/verify_linux_atspi.py only."""
import json
from pathlib import Path
import sys

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
items=Gtk.ListBox()
items.set_selection_mode(Gtk.SelectionMode.SINGLE)
row=Gtk.ListBoxRow()
row.get_accessible().set_name("Item")
row.add(Gtk.Label(label="Item"))
items.add(row)
box.pack_start(items,False,False,0)
panel=Gtk.EventBox()
panel.get_accessible().set_name("Pointer panel")
panel.set_size_request(500,90)
panel.add(Gtk.Label(label="Native pointer area"))
panel.add_events(Gdk.EventMask.BUTTON_PRESS_MASK|Gdk.EventMask.BUTTON_RELEASE_MASK|
                 Gdk.EventMask.POINTER_MOTION_MASK|Gdk.EventMask.ENTER_NOTIFY_MASK|
                 Gdk.EventMask.SCROLL_MASK)
press=None
def pointer(_,event):
    global press
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
    if command_path.exists():
        data=json.loads(command_path.read_text())
        op=data["op"]
        if op=="move": window.move(data["x"],data["y"])
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
