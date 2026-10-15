// CI-only controller. Never installed by qcu.gnome_bridge or included in wheels.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import St from 'gi://St';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
const XML = '<node><interface name="org.qcu.Test1"><method name="Request"><arg type="s" direction="in"/><arg type="s" direction="out"/></method></interface></node>';
export default class TestControl extends Extension {
    enable() {
        this.actors = [];
        this.object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this.object.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/QCUTest');
    }
    disable() {
        this.object.unexport();
        for (const actor of this.actors)
            actor.destroy();
        this.actors = [];
    }
    Request(payload) {
        const r = JSON.parse(payload);
        if (!r.title.startsWith('QCU Linux fixture '))
            throw new Error('Owned fixture title required');
        const windows = global.get_window_actors().map(a => a.meta_window);
        const w = windows.find(v => v.get_pid() === r.pid && v.get_title() === r.title);
        if (!w)
            throw new Error('Fixture window not found');
        if (r.op === 'focus' || r.op === 'clear') {
            for (const actor of this.actors)
                actor.destroy();
            this.actors = [];
            Main.overview.hide();
            w.activate(global.get_current_time());
        } else if (r.op === 'move') {
            w.move_frame(true, r.x, r.y);
        } else if (r.op === 'overlay') {
            const actor = new St.Widget({reactive: true, style: 'background-color: red;',
                x: r.x-30, y: r.y-30, width: 100, height: 100});
            Main.layoutManager.addChrome(actor);
            this.actors.push(actor);
        } else if (r.op === 'other') {
            const other = windows.find(v => v.get_pid() === r.pid && v.get_title() === 'QCU owned other window');
            if (!other)
                throw new Error('Other fixture window not found');
            other.activate(global.get_current_time());
        }
        return JSON.stringify({ok: true, bounds: w.get_frame_rect()});
    }
}
