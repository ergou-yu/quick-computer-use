// Native bridge for GNOME 46. Installation/enabling is an explicit user action.
import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';
import * as Main from 'resource:///org/gnome/shell/ui/main.js';

const XML = `<node><interface name="org.qcu.Wayland1">
<method name="Request"><arg type="s" direction="in"/><arg type="s" direction="out"/></method>
</interface></node>`;
const rect = r => ({x: r.x, y: r.y, width: r.width, height: r.height});
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
function fail(reason, message) { throw Object.assign(new Error(message), {reason}); }
function finite(v) { return typeof v === 'number' && Number.isFinite(v); }
function tick() {
    return new Promise(resolve => GLib.idle_add(GLib.PRIORITY_DEFAULT_IDLE, () => {
        resolve();
        return GLib.SOURCE_REMOVE;
    }));
}

export default class QCUBridge extends Extension {
    enable() {
        this._instance = GLib.uuid_string_random();
        this._bindings = new Map();
        this._busy = false;
        this._enabled = true;
        this._pointer = this._keyboard = null;
        this._object = Gio.DBusExportedObject.wrapJSObject(XML, this);
        this._object.export(Gio.DBus.session, '/org/gnome/Shell/Extensions/QCU');
    }

    disable() {
        this._enabled = false;
        this._object?.unexport();
        this._object = null;
        this._bindings.clear();
        // Pending requests release their own held keys/buttons in finally.
        if (!this._busy)
            this._pointer = this._keyboard = null;
    }

    async RequestAsync([payload], invocation) {
        let answer;
        try {
            if (payload.length > 100000)
                fail('invalid_request', 'Request too large');
            const r = JSON.parse(payload);
            if (!Meta.is_wayland_compositor())
                fail('session_mismatch', 'GNOME bridge requires Wayland');
            if (r.op === 'hello') {
                answer = {protocol: 1, instance: this._instance, session_type: 'wayland'};
            } else {
                if (!this._enabled || r.instance !== this._instance)
                    fail('target_changed', 'Bridge instance changed; observe and bind again');
                if (this._busy)
                    fail('input_busy', 'Another native request is pending');
                this._busy = true;
                try {
                    answer = await this._request(r, invocation.get_sender());
                } finally {
                    this._busy = false;
                    if (!this._enabled)
                        this._pointer = this._keyboard = null;
                }
            }
            answer = {ok: true, ...answer};
        } catch (e) {
            answer = {ok: false, reason: e.reason ?? 'wayland_error', error: e.message,
                dispatched: e.dispatched ?? false};
        }
        invocation.return_value(new GLib.Variant('(s)', [JSON.stringify(answer)]));
    }

    _windows() {
        return global.get_window_actors().map(a => a.meta_window);
    }

    _process(pid) {
        const [, bytes] = GLib.file_get_contents(`/proc/${pid}/stat`);
        const stat = new TextDecoder().decode(bytes);
        return {process_started: stat.slice(stat.lastIndexOf(')') + 2).split(/\s+/)[19],
            process_image: GLib.file_read_link(`/proc/${pid}/exe`)};
    }

    _window(binding, sender) {
        const b = this._bindings.get(binding?.token);
        if (!b || b.sender !== sender || !same(binding, b.binding))
            fail('target_mismatch', 'Binding belongs to another caller or window');
        const w = this._windows().find(v => String(v.get_stable_sequence()) === b.binding.window_id);
        if (!w || w.get_pid() !== b.target.pid || !same(this._process(w.get_pid()), b.process))
            fail('target_changed', 'Window or process lifetime changed');
        if (!w.get_compositor_private()?.visible || w.minimized || !w.showing_on_its_workspace() ||
            !w.located_on_workspace(global.workspace_manager.get_active_workspace()))
            fail('window_not_visible', 'Bound window is not visible');
        return w;
    }

    _geometry(w) {
        const frame = w.get_frame_rect();
        const g = rect(frame);
        if (g.width <= 0 || g.height <= 0 || g.width > 8192 || g.height > 8192 || g.width*g.height > 16000000)
            fail('capture_too_large', 'Invalid or oversized target');
        return {bounds: g, client_bounds: rect(w.frame_rect_to_client_rect(frame))};
    }

    _foreground(w) {
        if (Main.sessionMode.isLocked || Main.overview.visible || Main.modalCount > 0)
            fail('foreground_mismatch', 'Lock screen, overview or Shell modal is active');
        if (global.display.focus_window !== w || global.stage.get_key_focus() !== null)
            fail('foreground_mismatch', 'Focus is outside the bound window; QCU never activates it');
    }

    _point(w, g, x, y) {
        if (!finite(x) || !finite(y))
            fail('invalid_coordinates', 'Coordinates must be finite numbers');
        if (x < g.x || y < g.y || x >= g.x+g.width || y >= g.y+g.height)
            fail('outside_target', 'Pointer is outside the bound window');
        let actor = global.stage.get_actor_at_pos(Clutter.PickMode.ALL, x, y);
        for (let i = 0; actor && i < 64; i++, actor = actor.get_parent()) {
            if (actor.meta_window === w)
                return [x, y];
        }
        fail('point_occluded', 'Another surface or Shell UI covers this point');
    }

    _keys(key) {
        if (typeof key !== 'string' || !key)
            fail('invalid_key', 'Expected a named key or shortcut');
        const aliases = {Ctrl: 'Control_L', Control: 'Control_L', Alt: 'Alt_L', Shift: 'Shift_L',
            Super: 'Super_L', Meta: 'Super_L', Enter: 'Return', Backspace: 'BackSpace',
            Space: 'space', PageDown: 'Next', PageUp: 'Prior'};
        const parts = key.split('+');
        if (parts.length > 5 || parts.slice(0, -1).some(p => !['Ctrl', 'Control', 'Alt', 'Shift', 'Super', 'Meta'].includes(p)))
            fail('unsupported_key', 'Unsupported shortcut');
        const codes = parts.map(p => Clutter[`KEY_${aliases[p] ?? p}`]);
        if (codes.some(c => !Number.isInteger(c)) || new Set(codes).size !== codes.length)
            fail('unsupported_key', 'Unknown or duplicate key; use AT-SPI fill for Unicode');
        return codes;
    }

    async _request(r, sender) {
        if (r.op === 'bind') {
            const t = r.target;
            if (!Number.isInteger(t?.pid) || t.pid <= 0 || typeof r.title !== 'string')
                fail('invalid_target', 'Expected an AT-SPI process and title');
            const process = this._process(t.pid);
            if (String(t.process_started) !== process.process_started || t.process_image !== process.process_image)
                fail('target_changed', 'AT-SPI process lifetime/executable changed');
            const candidates = this._windows().filter(w => w.get_pid() === t.pid && w.get_title() === r.title);
            if (candidates.length !== 1)
                fail('wayland_window_ambiguous', 'AT-SPI PID/title must match exactly one Mutter window');
            const binding = {token: GLib.uuid_string_random(), window_id: String(candidates[0].get_stable_sequence())};
            // Bound memory per connected worker, even for repeated observations.
            for (const [token, b] of this._bindings) {
                if (b.sender === sender)
                    this._bindings.delete(token);
            }
            if (this._bindings.size >= 64)
                fail('input_busy', 'Too many bridge clients');
            this._bindings.set(binding.token, {sender, binding, process, target: t});
            return {binding};
        }
        const w = this._window(r.binding, sender);
        const {bounds: g, client_bounds: client} = this._geometry(w);
        if (r.op === 'geometry')
            return {bounds: g, client_bounds: client};
        this._foreground(w);
        if (r.op === 'capture') {
            const stream = Gio.MemoryOutputStream.new_resizable();
            const screenshot = new Shell.Screenshot();
            try {
                await new Promise((resolve, reject) => screenshot.screenshot_window(true, false, stream, (s, result) => {
                    try { s.screenshot_window_finish(result); resolve(); } catch (e) { reject(e); }
                }));
                this._foreground(this._window(r.binding, sender));
                if (!same(this._geometry(w).bounds, g))
                    fail('target_changed', 'Window moved during capture');
                stream.close(null);
                const bytes = stream.steal_as_bytes();
                if (bytes.get_size() > 32000000)
                    fail('capture_too_large', 'PNG exceeds bridge limit');
                return {bounds: g, png: GLib.base64_encode(bytes.get_data())};
            } finally {
                if (!stream.is_closed())
                    stream.close(null);
            }
        }
        if (r.op !== 'input')
            fail('unsupported', 'Unknown bridge operation');
        return this._input(r, w, g, sender);
    }

    async _input(r, w, g, sender) {
        const p = r.params ?? {}, kind = r.kind;
        let points = [], keys = [], buttons = [], scroll = [];
        if (kind === 'press_key') {
            keys = this._keys(p.key);
        } else {
            if (kind === 'drag') {
                const a = p.from ?? {x: p.x1 ?? p.x, y: p.y1 ?? p.y};
                const b = p.to ?? {x: p.x2, y: p.y2};
                this._point(w, g, a.x, a.y);
                this._point(w, g, b.x, b.y);
                const n = p.steps ?? 8;
                if (!Number.isInteger(n) || n < 2 || n > 64)
                    fail('invalid_action', 'Drag steps must be in 2..64');
                points = Array.from({length: n+1}, (_, i) => this._point(w, g, a.x+(b.x-a.x)*i/n, a.y+(b.y-a.y)*i/n));
            } else {
                points = [this._point(w, g, p.x, p.y)];
            }
            if (['click', 'right_click', 'double_click'].includes(kind))
                buttons = Array(kind === 'double_click' ? 2 : 1).fill(kind === 'right_click' ? 3 : 1);
            else if (kind === 'scroll') {
                for (const [axis, positive, negative] of [['dy', Clutter.ScrollDirection.DOWN, Clutter.ScrollDirection.UP],
                    ['dx', Clutter.ScrollDirection.RIGHT, Clutter.ScrollDirection.LEFT]]) {
                    const delta = p[axis] ?? 0;
                    if (!finite(delta) || Math.abs(delta) > 2400)
                        fail('invalid_action', 'Wheel deltas must be finite and at most 2400');
                    scroll.push(...Array(Math.ceil(Math.abs(delta)/120)).fill(delta > 0 ? positive : negative));
                }
                if (!scroll.length)
                    fail('invalid_action', 'A nonzero wheel delta is required');
            } else if (!['hover', 'drag'].includes(kind)) {
                fail('unsupported', 'Unsupported native action');
            }
        }
        const mask = global.get_pointer()[2];
        const busy = Clutter.ModifierType.SHIFT_MASK | Clutter.ModifierType.CONTROL_MASK |
            Clutter.ModifierType.MOD1_MASK | Clutter.ModifierType.SUPER_MASK |
            Clutter.ModifierType.BUTTON1_MASK | Clutter.ModifierType.BUTTON2_MASK | Clutter.ModifierType.BUTTON3_MASK;
        if (mask & busy)
            fail('input_busy', 'A pointer button or modifier is already held');
        const seat = Clutter.get_default_backend().get_default_seat();
        if (keys.length)
            this._keyboard ??= seat.create_virtual_device(Clutter.InputDeviceType.KEYBOARD_DEVICE);
        else
            this._pointer ??= seat.create_virtual_device(Clutter.InputDeviceType.POINTER_DEVICE);
        let sent = false, events = 0, heldKeys = [], heldButtons = [];
        const send = async fn => {
            if (!this._enabled)
                fail('target_changed', 'Bridge disabled');
            this._foreground(this._window(r.binding, sender));
            if (!same(this._geometry(w).bounds, g))
                fail('target_changed', 'Window moved during input');
            sent = true;
            fn(GLib.get_monotonic_time());
            events++;
            await tick();
        };
        try {
            for (const code of keys) {
                heldKeys.push(code);
                await send(t => this._keyboard.notify_keyval(t, code, Clutter.KeyState.PRESSED));
            }
            for (const code of [...keys].reverse()) {
                await send(t => this._keyboard.notify_keyval(t, code, Clutter.KeyState.RELEASED));
                heldKeys.pop();
            }
            for (let i = 0; i < points.length; i++) {
                const [x, y] = points[i];
                this._point(w, g, x, y);
                await send(t => this._pointer.notify_absolute_motion(t, x, y));
                // Delivery can be asynchronous; wait only on the observed pointer.
                const deadline = GLib.get_monotonic_time()+500000;
                while (Math.abs(global.get_pointer()[0]-x) > 1 || Math.abs(global.get_pointer()[1]-y) > 1) {
                    if (GLib.get_monotonic_time() > deadline)
                        fail('pointer_mismatch', 'Mutter pointer did not reach the requested position');
                    await tick();
                }
                this._point(w, g, x, y);
                if (kind === 'drag' && i === 0) {
                    heldButtons.push(1);
                    await send(t => this._pointer.notify_button(t, 1, Clutter.ButtonState.PRESSED));
                }
            }
            if (kind === 'drag') {
                await send(t => this._pointer.notify_button(t, 1, Clutter.ButtonState.RELEASED));
                heldButtons = [];
            }
            for (const button of buttons) {
                this._point(w, g, ...points[0]);
                heldButtons.push(button);
                await send(t => this._pointer.notify_button(t, button, Clutter.ButtonState.PRESSED));
                await send(t => this._pointer.notify_button(t, button, Clutter.ButtonState.RELEASED));
                heldButtons = [];
            }
            for (const direction of scroll)
                await send(t => this._pointer.notify_discrete_scroll(t, direction, Clutter.ScrollSource.WHEEL));
            const [x, y] = global.get_pointer();
            return {events, pointer_position: {x, y}};
        } catch (e) {
            e.dispatched = sent;
            throw e;
        } finally {
            // Release only this request's attempted presses. No retry/failover.
            try {
                for (const code of heldKeys.reverse())
                    this._keyboard.notify_keyval(GLib.get_monotonic_time(), code, Clutter.KeyState.RELEASED);
                for (const button of heldButtons)
                    this._pointer.notify_button(GLib.get_monotonic_time(), button, Clutter.ButtonState.RELEASED);
            } catch (e) {
                throw Object.assign(e, {dispatched: true, reason: 'outcome_unknown'});
            }
        }
    }
}
