# Desktop (macOS permissions) setup

The macOS path uses Apple's Accessibility API (`AXUIElementRef` etc.)
through PyObjC, plus `CGEvent*` for input synthesis and
`CGWindowListCreateImage` for screenshots. It needs **explicit user
consent** on every Mac since 10.14 Mojave. Without the relevant grants,
calls fail and the router falls back to the (much slower, less accurate)
screenshot layer.

## The three TCC permissions you need

Modern macOS (Catalina+; **especially Sequoia / Tahoe / darwin 23+**)
splits desktop automation consent across **three separate** TCC
services. Granting one does NOT cover the others — this is the root
cause of "QCU keeps asking for more permissions mid-task":

| Permission | What uses it | When QCU hits it |
|---|---|---|
| **Accessibility** | `AXUIElement*`, `CGEventCreate*` mouse/keys | first `observe` / click / `press_key` |
| **Input Monitoring** | `CGEventPost` keystroke synthesis | first `press_key` / `fill` (Sequoia+ split it out from Accessibility; AX alone will NOT satisfy it) |
| **Screen Recording** | `CGWindowListCreateImage` | first `screenshot`, or whenever the router falls back to the screenshot layer |

> ⚠ **Screen Recording grants only take effect after the QCU process is
> restarted.** After approving it, run `qcu session end` and then
> `qcu session start --context desktop` again. This is an Apple
> limitation; there is no way around it.

## One-time setup

1. **Install the Python extras** (the macOS path needs these):

   ```bash
   pip install -e ".[macos]"
   ```

   This adds `pyobjc-framework-ApplicationServices` and
   `pyobjc-framework-Quartz`. There is **no** PyPI package for IOKit
   bindings — the Input Monitoring probe reaches IOKit via `ctypes`
   against `/System/Library/Frameworks/IOKit.framework`, so no extra
   install is needed for that.

2. **Probe what's missing** (prints a JSON report for all three):

   ```bash
   qcu doctor
   ```

   For each missing permission it also prints an `open '...'` hint to
   stderr and attempts to open the relevant Privacy pane directly.

3. **Grant everything up front with `session start`** — starting a
   desktop session runs a permission preflight and surfaces the macOS
   consent dialogs for anything missing, **before** the task loop runs,
   so `observe` / `act` / `screenshot` / `press_key` are not interrupted
   by separate prompts:

   ```bash
   qcu session start --context desktop
   ```

   The response JSON carries a `permissions` block:

   ```json
   {"ok": true, "resumed": false, "session": {...},
    "permissions": {
      "accessibility":     {"granted": true,  "url": "...Privacy_Accessibility"},
      "input_monitoring":  {"granted": false, "url": "...Privacy_ListenEvent"},
      "screen_recording":  {"granted": true,  "url": "...Privacy_ScreenCapture"}
    }}
   ```

   Missing permissions are surfaced via stderr (e.g.
   `Missing macOS permission — Input Monitoring: run open '<url>'`) but
   do **not** block session creation — you still get a session; you just
   won't be able to run the affected actions until you grant.

4. If you prefer to do it by hand instead: **System Settings → Privacy
   & Security**, and add the binary the agent runs as (your Python
   interpreter, or the launching parent app — iTerm/Terminal/your IDE —
   macOS ties permission to the launching parent) to each of the three
   panes above.

## Resetting all three consents

```bash
sudo tccutil reset Accessibility
sudo tccutil reset ListenEvent        # Input Monitoring
sudo tccutil reset ScreenCapture      # Screen Recording
```

Note: this works on personal installs. Managed/MDM-locked Macs may
ignore the request.

## What works after consent

- **Accessibility** → `observe` returns the focused app's element tree,
  normalized roles, and click coordinates in Quartz points (same unit as
  mouse events — no scale math on Retina); `click` does
  `AXUIElementPerformAction(...,"AXPress")`; `fill` does
  `AXSetAttributeValue(elem,"AXValue",…)`.
- **Input Monitoring** → `press_key` and combo keys (`Cmd+Q`, `Cmd+Tab`,
  etc.) land real synthesized keystrokes via `CGEventPost`.
- **Screen Recording** → `screenshot` / `CGWindowListCreateImage`
  returns the on-screen windows (instead of an empty/black image).

## Known limitations in this MVP

- `type` is stubby; for real Unicode input integrate
  `Quartz.CGEventCreateKeyboardEvent` with `CGEventKeyboardSetUnicodeString`
  or use the clipboard.
- `_screenshot()` uses `CGWindowListCreateImage` and **cannot capture
  the screen while Secure Input is active** (1Password password prompts,
  the macOS lock screen, etc.).
- The Screen Recording probe checks whether the captured image has a
  non-zero width/height (a denied/undetermined consent returns an empty
  image, not a black one). This is reliable on real hardware.

## Troubleshooting

| symptom | fix |
|---|---|
| `AXIsProcessTrusted()` returns False | Re-grant Accessibility; `qcu doctor` shows the pane URL. |
| First `qcu observe` returns "Accessibility permission not granted" | Run `qcu session start --context desktop` (it triggers the prompt via the preflight), not just `observe`. |
| `press_key`/`fill` silently does nothing | Missing **Input Monitoring** (not just Accessibility). `qcu doctor` reports it; grant then re-run. |
| Screenshot returns black / "image is None" | Missing **Screen Recording**. Grant it, then `qcu session end` + `session start` (restart required — Apple limitation). |
| Coordinate clicks land off-target | The focused app might have a custom coordinate space; try `qcu act '{"type":"click","params":{"x":...,"y":...}}'` to verify mouse injection works. |
| Clicks do nothing at all | `sudo tccutil reset Accessibility`, restart Terminal/iTerm, `qcu session start --context desktop` to re-grant. |
| Screenshot returns black (Secure Input) | Secure Input is active in another app. Close it and retry. |

## Strict-background mode (`--strict-background`)

Real-world problem: when an agent drives QCU while the user is actively
working, `activateWithOptions_` / `open -a` rip focus to the target app,
and between QCU calls the frontmost app drifts (Finder→飞书→Clash was seen
in production), so `observe` ends up inspecting the wrong app entirely.

```bash
qcu session start --context desktop --strict-background
```

This flag makes the session **fail-fast on focus theft and drift**:

- **`launch_app` / `activate_app` are refused** outright (`ok=false`,
  message names the mode). These can't be made background-safe without a
  larger refactor that drives apps purely via AX (no window activation).
  Until then, use AX-direct actions (`fill`/`click` on a ref) instead.
- **Every other action aborts if the frontmost app has drifted** from the
  one captured at session start (`ok=false`, message shows expected vs
  actual). This turns silent "I clicked the wrong app" into a loud failure.
- **`observe` does not abort** (it never steals focus), but it surfaces
  `routing_meta.strict_background_drift=true` with the expected/actual
  names so the caller knows the tree may belong to the wrong app.

What this mode does **NOT** do: it does not enable true background control
(acting on a non-frontmost app without ever activating it). That requires
the AX-without-activation refactor and is future work. The flag is an
honest guardrail, not a capability.

