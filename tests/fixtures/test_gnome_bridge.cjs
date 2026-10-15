// Portable failure-boundary tests against the actual extension implementation.
// GNOME/Clutter delivery is verified separately in the headless compositor job.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
let source = fs.readFileSync(path.join(__dirname, '../../qcu/assets/gnome/qcu@quick-computer-use/extension.js'), 'utf8');
source = source.replace(/^import .*;\r?\n/gm, '').replace('export default class QCUBridge', 'class QCUBridge');
const events = [];
let pointer = [30, 30, 0], clock = 1, onKey = () => {};
const keyboard = {notify_keyval(t, key, state) { events.push(['key', key, state]); onKey(key, state); }};
const mouse = {
    notify_absolute_motion(t, x, y) { events.push(['move', x, y]); pointer = [x, y, 0]; },
    notify_relative_motion(t, x, y) { events.push(['relative', x, y]); pointer = [pointer[0]+x, pointer[1]+y, 0]; },
    notify_button(t, button, state) { events.push(['button', button, state]); },
    notify_discrete_scroll(t, direction) { events.push(['scroll', direction]); },
};
const Clutter = {KEY_Control_L: 65507, KEY_a: 97, KEY_F6: 65475,
    KeyState: {PRESSED: 1, RELEASED: 0}, ButtonState: {PRESSED: 1, RELEASED: 0},
    InputDeviceType: {KEYBOARD_DEVICE: 1, POINTER_DEVICE: 2}, PickMode: {ALL: 1},
    ModifierType: {SHIFT_MASK: 1, CONTROL_MASK: 4, MOD1_MASK: 8, MOD4_MASK: 64, SUPER_MASK: 67108864,
        BUTTON1_MASK: 256, BUTTON2_MASK: 512, BUTTON3_MASK: 1024, BUTTON4_MASK: 2048, BUTTON5_MASK: 4096},
    ScrollDirection: {UP: 1, DOWN: 2, LEFT: 3, RIGHT: 4}, ScrollSource: {WHEEL: 1},
    get_default_backend: () => ({get_pointer_sprite: () => ({get_coords: () => ({x: 41, y: 73})}),
        get_default_seat: () => ({create_virtual_device: k => k === 1 ? keyboard : mouse,
        warp_pointer: (x, y) => { events.push(['warp', x, y]); pointer = [x, y, 0]; }})}),
};
const globalMock = {get_pointer: () => pointer};
const main = {sessionMode: {isLocked: false}, overview: {visible: false}, modalCount: 0};
const context = {Clutter, Gio: {}, GLib: {get_monotonic_time: () => clock++, SOURCE_REMOVE: false,
    idle_add(p, fn) { queueMicrotask(fn); }}, Meta: {}, Shell: {}, Main: main,
    Extension: class {}, global: globalMock, console, TextDecoder, Bridge: null,
    Config: {PACKAGE_VERSION: '50'}};
vm.runInNewContext(source + '\nBridge = QCUBridge;', context);
const Bridge = context.Bridge;
const w = {}, g = {x: 10, y: 10, width: 300, height: 200};
function bridge() {
    events.length = 0;
    pointer = [30, 30, 0];
    onKey = () => {};
    const b = new Bridge();
    b._enabled = true;
    b._window = () => w;
    b._foreground = () => {};
    b._geometry = () => ({bounds: g});
    b._point = (window, bounds, x, y) => [x, y];
    b._pointerPosition = () => pointer.slice(0,2);
    return b;
}
const input = (b, kind, params) => b._input({kind, params, binding: {}}, w, g, ':1.2');
(async () => {
    let checks = 0;
    let b = bridge();
    onKey = (key, state) => { if (key === 97 && state === 1) throw new Error('late failure'); };
    await assert.rejects(input(b, 'press_key', {key: 'Ctrl+a'}), e => e.dispatched === true);
    assert.deepEqual(events, [['key', 65507, 1], ['key', 97, 1], ['key', 97, 0], ['key', 65507, 0]]);
    checks++;
    b = bridge();
    b._foreground = () => { throw Object.assign(new Error('focus moved'), {reason: 'foreground_mismatch'}); };
    await assert.rejects(input(b, 'press_key', {key: 'F6'}), e => e.dispatched === false);
    assert.equal(events.length, 0); // preflight failure must not release an unattempted key
    checks++;
    b = bridge();
    b._geometry = () => ({bounds: {...g, x: 90}});
    await assert.rejects(input(b, 'press_key', {key: 'F6'}), e => e.dispatched === false);
    assert.equal(events.length, 0);
    checks++;
    for (const mask of [4, 64, 2048]) {
        b = bridge();
        pointer[2] = mask;
        await assert.rejects(input(b, 'click', {x: 20, y: 20}), e => e.reason === 'input_busy');
        assert.equal(events.length, 0);
    }
    checks++;
    b = bridge();
    b._point = () => { throw Object.assign(new Error('occluded'), {reason: 'point_occluded'}); };
    await assert.rejects(input(b, 'click', {x: 20, y: 20}), e => e.reason === 'point_occluded');
    assert.equal(events.length, 0);
    checks++;
    b = bridge();
    await assert.rejects(input(b, 'scroll', {x: 20, y: 20, dy: 1e9}), e => e.reason === 'invalid_action');
    assert.equal(events.length, 0);
    checks++;
    b = bridge();
    await assert.rejects(input(b, 'press_key', {key: 'Ctrl+Unknown'}), e => e.reason === 'unsupported_key');
    assert.equal(events.length, 0);
    checks++;
    b = bridge();
    onKey = () => { b._enabled = false; };
    await assert.rejects(input(b, 'press_key', {key: 'F6'}), e => e.dispatched === true);
    assert.deepEqual(events, [['key', 65475, 1], ['key', 65475, 0]]);
    checks++;
    const stage = {get_key_focus: () => stage};
    globalMock.stage = stage;
    globalMock.display = {focus_window: w};
    Bridge.prototype._foreground.call({}, w); // Clutter returns the stage for default focus
    stage.get_key_focus = () => ({shellMenu: true});
    assert.throws(() => Bridge.prototype._foreground.call({}, w), e => e.reason === 'foreground_mismatch');
    checks++;
    context.GLib.Variant = class { constructor(signature, value) { this.value = value; } };
    let reply;
    await new Bridge().RequestAsync([JSON.stringify({op: 'hello'})], {return_value: v => { reply = JSON.parse(v.value[0]); }});
    assert.equal(reply.ok, true); // GNOME 50 has no Meta.is_wayland_compositor
    assert.equal(reply.session_type, 'wayland');
    context.Meta.is_wayland_compositor = () => false;
    await new Bridge().RequestAsync([JSON.stringify({op: 'hello'})], {return_value: v => { reply = JSON.parse(v.value[0]); }});
    assert.equal(reply.ok, false); // older GNOME X11 must still be refused
    assert.equal(reply.reason, 'session_mismatch');
    checks++;
    b = bridge();
    const hover = await input(b, 'hover', {x: 20, y: 30});
    assert.deepEqual(events, [['move', 20, 30]]);
    assert.equal(hover.pointer_motion, 'virtual_absolute');
    checks++;
    const position = Bridge.prototype._pointerPosition.call({});
    assert.equal(position[0], 41);
    assert.equal(position[1], 73);
    checks++;
    console.log(`${checks} GNOME bridge failure-boundary checks passed (mock host)`);
})().catch(e => { console.error(e); process.exitCode = 1; });
