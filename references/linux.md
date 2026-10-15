# Linux AT-SPI2 and X11 — 1.10.0

Linux desktop uses `desktop_linux`. Normal reads and ref actions go directly
through AT-SPI2 on one persistent worker. Web DOM/CDP, compact observations,
cached find/inspect, condition polling and bounded batches retain their paths.
Native X11 libraries are loaded only for an explicit screenshot/input request.

## Install and select a target

Ubuntu/Debian system Python example (the CI target is Ubuntu 24.04/Python 3.12):

```bash
sudo apt-get install python3-venv python3-gi gir1.2-atspi-2.0 at-spi2-core
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install -e '.[linux,dev]'
./scripts/qcu doctor
./scripts/qcu session start --context desktop
./scripts/qcu observe --pid 12345 --window 'Selected title' --compact
```

Use the desktop user's Python, DBus session and accessibility bus. A root/SSH
shell, container or unrelated DBus session cannot automatically access the
user's controls. Custom Python versions must have matching GI bindings;
installing the `linux` pip extra alone does not install system GI/AT-SPI.
The Bash shim uses `QCU_PYTHON`, the source `.venv/bin/python`, or `python3`,
in that order; it can be invoked outside the repository directory.

Omit desktop extras for web-only use; install Playwright Chromium separately
for web/CDP. Other distributions need their equivalent GI/AT-SPI packages.
Applications must expose accessible controls; toolkit or sandbox limitations
are reported on observation rather than inferred from package availability.

On Ubuntu versions restricting unprivileged user namespaces, bundled Chromium
may fail with `No usable sandbox`. Follow the [Chromium AppArmor guide](https://chromium.googlesource.com/chromium/src/+/main/docs/security/apparmor-userns-restrictions.md)
for the browser executable. QCU does not silently add `--no-sandbox` or alter
host policy. The disposable CI runner loads a profile for its exact test binary.

Observe requires an explicit PID, title/app constraint, or a previous bound
window. Ambiguous windows fail with candidates. `window_id` is the unique
AT-SPI DBus owner plus object path, not an XID. Refs additionally encode the
backend lifetime, process start, session and observation generation. A new
observation, restarted app/backend or changed window requires fresh refs.

## Semantic operations

```json
{"type":"fill","params":{"ref":"<editable-ref>","text":"Linux 中文 🚀"}}
{"type":"click","params":{"ref":"<button-ref>","verify":{"kind":"text","equals":"Saved"}}}
{"type":"toggle","params":{"ref":"<checkbox-ref>","verify":{"kind":"checked","ref":"<checkbox-ref>","equals":true}}}
{"type":"select","params":{"ref":"<item-ref>","verify":{"kind":"selected","ref":"<item-ref>","equals":true}}}
```

`fill` uses EditableText with value readback. Click/invoke/toggle resolves a
known AT-SPI Action; select uses the parent Selection interface. Unsupported,
disabled, defunct, readonly or reparented controls fail before sending.
`type` inserts Unicode at a focused EditableText caret with no active text
selection. Prefer `fill` for replacement; QCU never edits the global X11 keymap
or clipboard to inject arbitrary Unicode. No action is automatically replayed.

## X11 native fallback

The pip `linux` extra supplies python-xlib. Use an EWMH window manager and a
viewable TrueColor RGB client. QCU matches exactly one client by the AT-SPI
window's PID/title and retains that XID. Subsequent checks verify the process
start/executable and client ownership; it never switches to a different client.

Screenshot uses the bound client's GetImage and PNG encoding. It returns the
X11 screen-pixel origin, scale 1, bounds, XID and the original AT-SPI identity.
Mapped windows intersecting the capture above the client's WM frame are
rejected. Minimized/unmapped clients, unknown visuals and large/invalid sizes
fail explicitly. This is screenshot capture, not image/OCR recognition.

```json
{"type":"screenshot","params":{"path":"/tmp/qcu-window.png"}}
{"type":"click","params":{"ref":"<button-ref>","input_mode":"native","verify":{"kind":"text","equals":"Saved"}}}
{"type":"press_key","params":{"ref":"<focused-ref>","key":"Ctrl+a"}}
{"type":"scroll","params":{"x":450,"y":300,"dy":120}}
{"type":"drag","params":{"from":{"x":450,"y":300},"to":{"x":490,"y":310}}}
```

Coordinates and refs above are placeholders; obtain them from the current
target. Native click/double/right click, hover, wheel, drag and named keys use
XTEST. Input requires the bound active window, owned keyboard focus, no held
buttons/modifiers and unobstructed pointer points. It never activates an app.
Refs use live AT-SPI bounds; ref/coordinate mixtures are rejected. Wheel deltas
are bounded to 2400 per axis, with 120 per notch (positive dy scrolls down).
Shortcuts use the existing keymap's base/shift group; missing keys and other
groups fail. Mixed DPI/monitor/keymap combinations need per-target validation.
After a partial send only this action's held events are released; no transport
switch or retry follows. Result evidence comes from AT-SPI postconditions.
Drag endpoints can also use `ref_from`/`ref_to` with live bounds. A ref click's
explicit `fallback:"native"` is honored only if a semantic interface/action is
unsupported before dispatch; a late AT-SPI error cannot trigger fallback.

## Wayland

Exposed AT-SPI controls remain usable for semantic reads/actions. Native
screenshot/input returns `wayland_native_unsupported`, even when XWayland has
a DISPLAY. A portal/compositor capture/remote-desktop adapter and its consent
flow are not implemented. QCU does not change compositor permissions or fall
back to another user's session. Browser DOM/CDP is independent of XTEST.

## Verification

The portable tests inject AT-SPI/X11 objects and do not prove interoperability.
`scripts/verify_linux_atspi.py` creates a disposable GTK3 window plus isolated
QCU_HOME in an existing Linux DBus/X11 session. It checks semantic operations,
Unicode, batch/readback, native capture/input, stale refs, focus/occlusion,
movement and subsequent semantic fill. Install `gir1.2-gtk-3.0` for the fixture.

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/verify_linux_atspi.py --output linux-live.json
```

The repository workflow runs this on Ubuntu with DBus, Xvfb and Openbox, and
uploads the report, logs, captures and packages. This is a real Linux virtual
desktop fixture, not physical-machine or Wayland compositor validation.

API references: [GNOME AT-SPI2](https://docs.gtk.org/atspi2/),
[EditableText](https://docs.gtk.org/atspi2/iface.EditableText.html),
[AT-SPI timeouts](https://docs.gtk.org/atspi2/func.set_timeout.html),
[python-xlib](https://github.com/python-xlib/python-xlib).
