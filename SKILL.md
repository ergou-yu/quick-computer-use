---
name: quick-computer-use
description: "Use when the user asks to drive a browser OR control a native desktop app — anything that needs real mouse/keyboard/window control instead of HTTP, curl, or static file reads. Covers BOTH: (a) web — go to this URL and click the button, fill out this checkout, automate a website, drive the browser; AND (b) desktop macOS apps — open Finder and move/rename a file, switch to Messages and reply, click the menubar icon, use keyboard shortcuts in any app, control Notes / TextEdit / Terminal / Keynote / System Settings, open an app and interact with it. Always prefers the accessibility tree (web ARIA + macOS AX API) over screenshots, which makes each step far faster and more accurate than vision-based computer use. NOT for plain HTTP/curl/scraping, image generation, or reading files you could open directly."
metadata:
  author: QCU contributors
  requires:
    bins: ["python3"]
    packages: ["playwright>=1.40"]
  cliHelp: "python3 -m qcu --version"
license: MIT
---

# Quick Computer Use (QCU)

A faster computer-use skill that **defaults to the accessibility tree** and only
falls back to screenshots when the a11y tree is genuinely blind (canvas / WebGL,
target ref missing, critical action needing visual confirmation).

## Capability Matrix — read this before promising anything (RTFM)

QCU runs on four distinct tiers. The `qcu observe` first call tells you which
tier you're on: look at `routing_meta.n_elements` (total interactive elements
seen) and `routing_meta.web_app` (non-null only for browser windows).

| Tier | When | What works | Per-step typical cost |
|---|---|---|---|
| **T1 Full** | Native macOS app with healthy AX tree: Finder, Mail, Notes, TextEdit, System Settings (most panels), Calendar, Contacts, Preview, Stickies, natively-AppKit cocoa apps | `click`, `fill`, `press_key`, `scroll`, full a11y read/write. **Each action is structurally verified** (silent no-op impossible). | 200–600 ms |
| **T2 Degraded** | SwiftUI / Electron app with anonymous (no-title) tree: System Settings panels that hide buttons in `AXGroup`, very old apps | Menu-item AXPress + coordinate fallback. Tree is sparse but enough to drive by name/path. | 300–800 ms |
| **T3 Web takeover** | Browser windows (Chrome / Safari / Edge / Brave / Arc) — `routing_meta.web_app` is populated with tab URLs | `qcu observe` returns *the chrome only* (window frame) plus per-tab URL+title via the browser's AppleScript dictionary. To actually act on page DOM, **restart QCU session in `--context web` and navigate the QCU-owned Chromium to the same URL**; you then get full CDP DOM at ~0.3 s/step. The user's existing browser is **never** touched. | T3a (tab listing) ~0.3 s · T3b (web session takeover) tied to T1 cost |
| **T4 Pixel** | Canvas / WebGL / Citrix / DirectX games / remote desktops | **Not supported.** Surfaced as "vision fallback pending"; for now, abort and tell the user. Vision ladder (V1 template / V2 OCR / V4 local small-VLM) is on the roadmap. | n/a |

**Decision rule for the LLM (must run after the first `observe`):**
1. `n_elements > 30` AND `web_app is None` → T1, proceed normally.
2. `n_elements ≤ 30` AND named buttons/menus still exist → T2, proceed but
   prefer `--role` / `--name` filtering and menu-item AXPress.
3. `web_app is not None` → T3; if the task needs DOM interaction, propose
   `session end` → `session start --context web` → `navigate <url>`.
4. Otherwise → T4; abort with "QCU doesn't support pixel-only targets here".

## Read First
- `references/api.md` — full `Observation` / `Action` schema, every CLI flag.
- `references/routing.md` — current rule table, decision log format, how to add a rule.
- `references/desktop.md` — macOS permission (Accessibility / Input Monitoring / Screen Recording) walkthrough, `qcu doctor`.

## Core Concepts
- **Observation** = snapshot of the current state. Contains a flat list of
  `elements` with stable `ref`s (e.g. `ref_42`), plus `raw_tree` (LLM-friendly
  text), `tools` (WebMCP, if any), and `screenshot_path` (only when the
  router chose the screenshot layer).
- **Action** = one command, e.g. `{"type":"click","params":{"ref":"ref_42"}}`.
- **Layer** = one control surface: `web_a11y`, `webmcp`, `desktop_ax`,
  `desktop_uia` (Windows stub), `screenshot_fallback`.
- **Router** = picks which layer handles the next action. Rule-based in this
  MVP; every decision is recorded to `~/.qcu/telemetry.jsonl` so a future ML
  classifier can replace the rule chain without changing call sites.

## Default Workflow (LLM agent loop)

QCU handles **two environments** through the same observe→act loop:

- **Web** (`--context web`, default): a long-lived Chromium daemon controlled via CDP.
- **Desktop macOS apps** (`--context desktop`): native apps driven through the macOS
  Accessibility (AX) API + CGEvent mouse/keyboard — NOT screenshots. launch_app/
  activate_app/click/type all work on TextEdit, Notes, Finder, Terminal, System
  Settings, etc.

```bash
# 1. One-time session setup — pick the environment.
qcu session start --context web        # browser automation (default)
qcu session start --context desktop    # native macOS app automation

# 2. Observe → choose → act → repeat.
#    Web:  each `qcu` call attaches to the SAME long-lived Chromium daemon, so
#          page focus / unsubmitted form input / a11y refs survive between calls.
#    Desktop: observe reads the focused app's AX tree; launch_app/activate_app
#          switch target apps (observe also lists all running apps).
obs=$(qcu observe)
# ... LLM reads $obs.elements + $obs.raw_tree, picks a ref ...
qcu act '{"type":"click","params":{"ref":"ref_42"}}'
obs=$(qcu observe)
qcu act '{"type":"fill","params":{"ref":"ref_17","text":"alice@example.com"}}'
qcu act '{"type":"press_key","params":{"key":"Enter"}}'

# 3. Done.
qcu session end                      # stops the daemon
```

Every observe/act call returns a JSON object that includes
`router_decision` (which layer ran, the priority, why) and `layer_used` (the
layer that actually executed). You usually don't need to inspect them — just
feed the observation back into the model.

## Browser daemon lifecycle

The browser is a **detached, long-lived Chromium process** controlled via the
Chrome DevTools Protocol (CDP), not a per-command Playwright launch:

- The first `observe`/`act`/`session start` that needs the web layer starts
  the daemon lazily (detached, so a crash or terminal close can't kill it),
  picks a free loopback port (9222+), and records `{browser_pid,
  browser_debug_port}` into `~/.qcu/session.json`.
- Every later `qcu` command attaches with Playwright's `connect_over_cdp` —
  no new browser, no DOM reset, so unsubmitted inputs and focus are preserved.
- `qcu session end` (or `qcu browser stop`) terminates the daemon by pid.
- `qcu browser status` reports whether the daemon is alive and on which port,
  without touching the session — useful for recovery.

Environment overrides: `QCU_HEADLESS=0` shows the window (default headless),
`QCU_BROWSER_CHANNEL=chrome|msedge` uses a system build, `QCU_USER_DATA_DIR`
overrides the profile (must be non-default — Chrome 136+ otherwise ignores the
debug port). The CDP endpoint is loopback-only and has no auth; never expose it.

## CLI

```text
qcu session start  [--context web|desktop|auto]
qcu session status
qcu session end                       # stops the browser daemon
qcu browser status                    # daemon health (port/pid/alive)
qcu browser stop                      # force-stop the daemon
qcu observe        [--max-depth N] [--layer NAME]
qcu act '<JSON>'   [--layer NAME]
qcu route <features.json | ->          # diagnostic: print routing decision
qcu stats          [--since 7d]
qcu schema observation
qcu schema action
qcu doctor                            # probe macOS TCC perms & hint missing ones
```

`<JSON>` for `act` is an Action object (see schema). Examples:

```bash
qcu act '{"type":"click","params":{"ref":"ref_42"}}'
qcu act '{"type":"fill","params":{"ref":"ref_17","text":"hello"}}'
qcu act '{"type":"press_key","params":{"key":"Enter"}}'
qcu act '{"type":"press_key","params":{"key":"Cmd+Q"}}'          # modifier combos: Cmd/Shift/Alt/Ctrl+key
qcu act '{"type":"navigate","params":{"url":"https://example.com"}}'
qcu act '{"type":"webmcp_call","params":{"tool":"submit_order","args":{}}}'
```

Desktop-specific actions (use with `--context desktop`):

```bash
qcu act '{"type":"launch_app","params":{"app":"TextEdit"}}'      # open/bring-to-front an app
qcu act '{"type":"activate_app","params":{"app":"Notes"}}'       # focus a running app
qcu act '{"type":"right_click","params":{"ref":"ref_5"}}'        # context menu
qcu act '{"type":"double_click","params":{"ref":"ref_5"}}'       # real double-click (Finder rename)
qcu act '{"type":"scroll","params":{"dy":300}}'                  # vertical scroll
```

## macOS permissions (desktop only)

Desktop automation on modern macOS (Catalina+, **especially Sequoia /
Tahoe / darwin 23+**) needs **three separate** TCC grants; granting one
does NOT cover the others, which is what causes "QCU keeps asking for
more permissions mid-task":

- **Accessibility** — `AXUIElement*`, mouse/keyboard `CGEventCreate*`.
- **Input Monitoring** — `CGEventPost` keystroke synthesis (split out
  from Accessibility on Sequoia+; AX alone does NOT satisfy it).
- **Screen Recording** — `CGWindowListCreateImage` screenshots.

`qcu session start --context desktop` runs a **permission preflight**:
it probes all three up front and surfaces the macOS consent dialogs for
anything missing **before** the task loop runs, so `observe` / `act` /
`screenshot` / `press_key` are not interrupted by separate prompts. The
response JSON carries a `permissions` block; missing ones are also
hinted on stderr. Run `qcu doctor` anytime to re-probe and get the
`open '<pane-url>'` commands.

> ⚠ **Screen Recording grants only take effect after the QCU process is
> restarted** — after granting it, run `qcu session end` then
> `qcu session start --context desktop` again. There is no way around
> this; it is an Apple limitation.

`launch_app` works immediately even before any grant. If Accessibility
is denied, `observe` returns `trusted: false` and clicks/types no-op.
See `references/desktop.md` for the full walkthrough.

## Desktop action priority — AX natives first, screenshots last

QCU drives native apps through the **macOS Accessibility (AX) API**, not vision.
Every action tries AX-native methods before any coordinate/keyboard fallback:

1. **AX-native (preferred)** — `fill` does `AXSetAttributeValue(elem,"AXValue",…)`
   directly (no click, no clipboard, no focus dependency); `click` does
   `AXUIElementPerformAction(elem,"AXPress")`; `right_click` prefers
   `AXShowMenu`; `double_click` presses twice. This is what makes buttons/search
   fields work even with no geometry and on background apps.
2. **CGEvent key/mouse (AX has no equivalent)** — `hover`, `scroll`, and
   `press_key` (incl. combos like `Cmd+Q`) use CGEvents. Coordinate clicks are
   used only when AXPress is unavailable for that element.
3. **Screenshot vision (true last resort)** — only when AX cannot reach the
   target at all (canvas / WebGL / hand-drawn UI). Never the default desktop
   path — QCU will not turn a desktop task into "look at the screen and guess".

Two enablers make this work broadly: observe sets `AXEnhancedUserInterface=True`
on the target app (so Notes/App Store/Music expose their window content, not
just the menu bar), and refs carry an AX fingerprint (role/subrole/name/path)
so a fresh CLI process can re-resolve the element and act on it natively.

## Coordinate-click safety gate (multi-monitor / occlusion)

When the AX-native path is unavailable for a click/drag and QCU falls back to
`CGEventPost` at the ref's cached `(cx, cy)`, it first runs a **safety gate**
that prevents the multi-monitor bug where the click landed on a higher-stacking
window (飞书 / Clash / a notification) that had moved over the cached point:

1. `window_at_point(x, y)` walks `CGWindowListCopyWindowInfo` (frontmost-first)
   to find the layer-0 window whose rect contains the point — that window is,
   by definition, the one that would receive the CGEvent.
2. `assert_click_target(app, x, y, app_window_bounds=…)` compares the owner
   name and the live target-window bounds against the cached point.
3. On mismatch the action is **aborted with `ok=false`** and a message that
   names the real owning window (e.g. "frontmost window at that point belongs
   to 飞书, not Mirroria"). No CGEvent is posted.

This is **fail-open**: if Quartz window-list introspection is unavailable the
gate returns `ok=true` so it never bricks clicks on a stripped-down host. The
gate is independent of `--strict-background` — it catches the wrong-window
case even when the foreground app is unchanged (a higher-stacking overlay on
the same desktop). Failures surface under `routing_meta` /
`LayerResult.data["click_safety"]` with `reason` ∈ `wrong_owner` /
`point_not_in_any_window` / `point_outside_target_window`.

## What the Router picks (rule chain)

| priority | rule | layer | when |
|---|---|---|---|
| 100 | canvas_target | screenshot_fallback | target sits on `<canvas>` / WebGL |
| 95  | visual_confirm | screenshot_fallback | action is in the critical list (e.g. `submit_payment`) |
| 80  | webmcp_tool    | webmcp            | page registered a `navigator.modelContext` tool |
| 70  | web_a11y       | web_a11y          | web context + ref in a11y tree (default happy path) |
| 66  | desktop_webview_blind | desktop_ax  | desktop app is an opaque AXWebArea shell (WKWebView/Electron) with `< 8` actable controls → hint T3 web takeover |
| 65  | desktop_ax     | desktop_ax        | desktop context + macOS AX trusted |
| 55  | desktop_no_ax  | screenshot_fallback | desktop but AX not granted |
| 40  | web_no_target  | screenshot_fallback | web + target ref missing |
| 10  | default        | screenshot_fallback | catch-all fallback |

Every decision (winner + every alternative) is appended to
`~/.qcu/telemetry.jsonl` so the training dataset for a future classifier
accumulates for free.

## Action Verification (no silent no-ops)

Every `click` / `fill` goes through an AX state-diff verifier before QCU
returns success. Architecture:

1. **Pre-snapshot** captures the target element's `AXValue` hash, children
   count, focused flag, parent AXChildren count, window title hash, etc.
2. The action fires (`AXPress` for clicks, `AXValue=` for fills).
3. **Post-poll loop** runs immediately (no upfront sleep) at 100 ms
   intervals, with per-action windows tuned by data:
   - `value_set` (fill): **400 ms** (measured median 52 ms, max 55 ms)
   - `value_flip` / `value_delta`: 400 ms
   - `button_press`, `children_change`, `element_death`: 1200 / 1500 / 1500 ms
   - `menu_open`, `window_change`: 2500 ms (SwiftUI modal cold-launch buffer)
4. **Early exit** the moment a signal matches; full window needed only for
   stub-noop detection.
5. **verified=no** (the action reported ok but had zero observable effect)
   triggers an **automatic P4 fallback**: the same click is re-tried as a
   coordinate event with re-verification. Both verdicts are surfaced in
   `routing_meta.verification` so the LLM can decide between retry,
   escalate, or switch to web takeover.
6. **telemetry** is appended to `~/.qcu/verify_telemetry.jsonl` one line per
   call (signal_class / matched_at_ms / samples) — the data source for
   future window tuning. Set `QCU_VERIFY_NO_TELEMETRY=1` to opt out.

Signals by action type:

| Action class | Signal watched |
|---|---|
| `click` AXCheckBox / AXRadioButton / AXSwitch | `AXValue` flips 0↔1 |
| `click` AXMenuItem (close/dismiss/quit) | target element disposed (`AXUIElementCopyAttributeValue` returns not-trusted) |
| `click` AXMenuItem (open panel/menu) | window title changes, or window count changes |
| `click` AXPopUpButton | target's `AXChildren` count changes |
| `click` AXSlider / AXIncrementor | `AXValue` numeric delta ≠ 0 |
| `click` AXButton (toolbar) | any of the above, fallback to focused/window change |
| `fill` AXTextField / AXTextArea | `AXValue` equals expected text (strict first, then whitespace + full/half-width normalize) |

## Honest Limitations (read this before promising speed)

### What is hard-capped by macOS / Chromium design (won't be fixed by code)

1. **Electron apps with AX tree collapsed** (Slack / Feishu / Notion / Discord
   / Cursor desktop / Obsidian, all build-2026): setting
   ``AXManualAccessibility=True`` returns ``err=0`` but the tree does NOT grow
   — it's a **stub no-op** verified on 8 separate Electron apps 2026-08-03.
   ``AXEnhancedUserInterface`` returns ``-25208 notImplemented``. Even
   registering ``AXObserver`` notifications is ack'd but doesn't kick the
   a11y pipeline. **There is no way for QCU to introspect Electron-app
   internal UI from outside the process.** Workaround: route to **T3 web
   takeover** — re-navigate the same URL in QCU's Chromium.
2. **Embedded WKWebView / AXWebArea shells** (e.g. Mirroria, and any AppKit
   app that hosts its UI inside a WKWebView): the AX tree exposes an
   ``AXWebArea`` node but **no DOM under it** — exactly the Electron symptom
   from outside the process. QCU now **detects** this: when observe hits an
   ``AXWebArea`` it sets ``routing_meta.web_view = {app, url: null, reason}``
   and prepends a raw_tree hint. When the tree is otherwise sparse
   (``n_interactive < 8``) the ``desktop_webview_blind`` rule (priority 66)
   fires and the decision reason tells the LLM to escalate to **T3 web
   takeover**. **QCU cannot auto-recover the page URL** — most WKWebView apps
   ship no scriptable tab dictionary the way Chrome/Safari do, so the user /
   LLM must supply the URL to navigate QCU's Chromium to. The Chromium daemon
   itself is fully functional (`qcu browser status`); the gap this closes is
   detection + escalation, not the daemon.
2. **Forcing a target app to become frontmost** against the user: there is
   no macOS public API for "front-app affinity". The best QCU can do is
   ``launch_app`` with ``NSWorkspaceLaunchConfigurationActivationKey=False``
   (which keeps target in background) and **trust** that no other app
   self-promotes. If a notification handler activates, QCU detects it via
   ``NSWorkspace.didActivateApplicationNotification`` and surfaces a
   checkpoint in routing_meta, but cannot restore focus silently.
3. **Forcing keyboard into a background window**: Quartz ``CGEventPost``
   dispatches to **whatever window is frontmost at dispatch time**, not to
   the AXFocused element. To type into a background TextEdit window, use
   ``fill`` (which goes ``AXValue=`` directly, no keyboard) — this is
   already QCU's default per upgrade 2.

### Session hard switches (`qcu session start`)

These flags are honest guardrails — they turn silent failures into loud ones.

- **`--strict-background`** (desktop): refuses ``launch_app``/``activate_app``
  (they steal focus) and aborts any action when the frontmost app has drifted
  from the one captured at session start. ``observe`` still runs but flags
  ``routing_meta.strict_background_drift=true``. This **detects** drift and
  **prevents** focus theft; it does **not** enable true background control
  (acting on a non-frontmost app without activation). That needs a larger
  AX-without-activation refactor and is future work. See
  ``references/desktop.md``.
- **`--no-screenshot-fallback`**: when the router would degrade an action to
  the ``screenshot_fallback`` layer (canvas target, missing ref, desktop
  without AX), the action is **refused** with ``ok=false`` instead of
  silently switching to vision. An explicit ``--layer screenshot_fallback``
  on a single ``act`` still overrides the flag (you asked for it directly).
  Use when the task must never rely on vision.

### Observe scoping (`qcu observe`)

- **`--app <name>`** / **`--pid <int>`**: observe a specific app instead of
  the frontmost one — essential when QCU runs in the background and focus has
  drifted to the caller.
- **`--window <substring>`**: walk only the ``AXWindow`` whose title contains
  the substring, dropping the menu bar and other windows. Cuts a 300-element
  tree down to the target window's ~30 controls.


### What QCU ships but is a known stub (Roadmap / T4)

4. **WebMCP** (`navigator.modelContext`) is a W3C proposal in 2026 — not yet
   shipped in any production browser. The detector and adapter are present,
   will activate automatically when sites start exposing tools.
5. **Vision ladder (T4)** — V2 OCR is **shipped**, the rest is roadmap:
   - V1 template matching (OpenCV `matchTemplate`, ~10–50 ms) — best for
     repeated UI (Feishu sidebar, game HUDs, Citrix). Deferred until V2
     hit-rate data shows where the cache misses cluster.
   - **V2 local OCR (Apple Vision `VNRecognizeTextRequest`) — SHIPPED.**
     Pipeline: resolve wid → **occlusion check (rect ∩ higher-stacking
     windows)** → optional activate self-heal → capture default-framing →
     downsample-if-large → Vision OCR → tie-break (exact > conf > area >
     ambiguous) → CGEvent click → pHash diff verify. Failure taxonomy is
     three-way non-collapsible: ``locate_fail`` (incl.
     ``reason="target_occluded"``) / ``act_fail`` (CGEvent dispatch raised)
     / ``verify_fail`` (clicked, but pre-vs-post diff was below
     ``HASH_VERIFY_FAIL_THRESHOLD=40``).
     **V2 envelope:** in-window visible text only. Menu-bar items are
     a separate SystemUIServer layer-0 window outside the target's rect
     — route those through T1 AX. Zero-disturbance is **not** a V2
     capability: when a target is occluded, the self-heal path *will*
     call ``activate_app`` and steal focus. The only true
     zero-disturbance path is the AX layer.
   - V3 icon detection (OmniParser-class small model, 200–500 ms) — fill
     gap between V2 (text) and V4 (vision)
   - V4 local small-VLM (MLX `Qwen2.5-VL-3B` or `UI-TARS-1.5-7B` quantized,
     800–1500 ms) — grounding when V1–V3 miss
   - V5 frontier VLM API (Claude/GPT-class, 2–5 s) — reserved for critical
     low-confidence cases only
6. **Windows** desktop layer is an interface stub only in this MVP; install
   ``uiautomation>=2.0`` and implement the v2 hooks to enable.
7. **Per-step target verify** consumes AX state; if the target's AX provider
   does notifiably change little (e.g. a settings panel that flips silently
   when you toggle a hidden state) verify can call NO even when the click
   worked. Failure-to-observe semantics is conservative.

### Performance claims (measured, not marketing)

8. **macOS native (T1)** per step: median 200–600 ms AXPress + 50–250 ms
   verify = **250–850 ms total**. Native manual click via trackpad is
   typically 300–800 ms for trained users, so we are at-or-below human
   on stock tasks.
9. **Web (T3b) via CDP** per step: 150–350 ms observe + 40–150 ms act =
   **200–500 ms** measured on example.com / Hacker News / news.ycombinator.com.
10. **Anthropic Computer Use** baseline per step: 3–6 s (screen capture +
    large VLM inference + remote network roundtrip). QCU's T1/T3 is
    ≈ 6–15× faster on equivalent tasks.

## Dependencies

- Python 3.11+
- `playwright>=1.40` (mandatory for the web path)
- macOS only: `pyobjc-framework-ApplicationServices`, `pyobjc-framework-Quartz`
- Windows only (v2): `uiautomation>=2.0`
- Dev: `pytest`, `pytest-asyncio`

Install:

```bash
pip install -e ".[macos,dev]"
python3 -m playwright install chromium     # or use the cached binary at
                                          # ~/Library/Caches/ms-playwright/
```

## File Layout

```
qcu/
├── SKILL.md                    # this file
├── pyproject.toml
├── README.md
├── references/                 # deep-dive docs (load on demand)
├── scripts/qcu                 # bash shim: ./scripts/qcu observe
├── qcu/                        # the Python package
│   ├── cli.py / cli_handlers.py / cli_schema.py
│   ├── session.py              # ~/.qcu/session.json lifecycle
│   ├── common/                 # Observation / Action / Element / Rect + normalize
│   ├── router/                 # features + rules + classifier
│   ├── layers/                 # web_a11y, webmcp, desktop_ax, desktop_uia, screenshot_fallback, browser_daemon
│   ├── grounding/              # GroundingModel Protocol stub
│   └── data/                   # JSONL telemetry writer + summary
├── tests/                      # unit + integration tests; pytest tests/
└── examples/                   # demo_router.py (no browser), demo_web_skeleton.py (Playwright)
```

## Common Mistakes

- Treating `Observation.elements` as the source of truth. The DOM can mutate
  between observe() and act(); the layer will try the locator first and fall
  back to the cached coordinate.
- Calling `act` on a ref from a previous observation without re-snapshotting
  — the ref may be invalidated by the page.
- Forgetting `qcu session end`. The daemon is detached and will keep running
  (headless by default) until you stop it; `qcu browser stop` force-kills it
  even if the session file is gone.
- Expecting a per-command browser launch. There is none — every command
  attaches to the shared daemon via CDP, which is exactly why unsubmitted
  form state and focus survive between `qcu` calls.
- Assuming WebMCP will speed things up today. It won't, until Chromium ships it.
- Launching an app by **name** when multiple copies exist (an installed copy
  in `/Applications` plus a build/packaging clone under
  `~/Library/Developer/Xcode/DerivedData`, `.build/`, or a staging dir). QCU
  sorts candidates with a `/Applications` preference and filters build noise,
  but when ambiguity remains it warns on stderr — pass the **absolute path**
  (`/Applications/Mirroria.app`) to disambiguate deterministically.
- A **multi-monitor secondary display placed to the left/above the primary**
  has negative coordinate origins in the global Quartz space. QCU's click-
  safety gate handles negative coords correctly, but if a click there is
  refused with `point_not_in_any_window` / `point_outside_target_window`,
  re-run `qcu observe` to refresh the cached window bounds — the snapshot was
  likely stale, not the math wrong.
- **Headed mode** (`QCU_HEADLESS=0`) navigation can stall when the page opens
  a permission dialog, interstitial, or native sheet — `_goto_with_retry`
  only retries transient network errors, so the page may stay on the previous
  URL while the session believes it navigated. If a headed navigation
  "succeeds" but the page didn't change, check for an open dialog or fall
  back to headless.
- An observe returning **0 elements + `routing_meta.trusted: false`** is a
  macOS Accessibility permission gap, NOT an empty UI. The same applies to
  `available: false` (Automation / Apple Events not granted, or pyobjc
  missing). QCU surfaces a remediation preface at the top of `raw_tree` and a
  stderr hint — read them. Recovery: run `qcu doctor`, grant in System
  Settings > Privacy & Security, then `qcu session end && qcu session start
  --context desktop`. Do not report "the app has no UI" without checking
  `trusted` / `available` first.

## Mandatory Post-Use Report

If QCU was invoked at least once during the current task, append a
`QCU 使用报告` section to the final user-facing response after the task result
and artifact links. Do this even when QCU failed, was interrupted by a human
approval boundary, or another method ultimately completed the task.

Base the report only on evidence from the current task, including QCU command
results, `routing_meta`, verification signals, errors, retries, and observable
UI outcomes. Do not invent praise, improvements, or bugs. Keep documented
limitations, environment or permission problems, and reproducible QCU defects
separate.

Use this compact structure:

```markdown
### QCU 使用报告

- 调用概况：说明 QCU 用于什么、采用的 context/tier/layer，以及成功、失败或回退情况。
- 优点：列出本次实际体现出的速度、准确性、结构化定位或验证能力；无充分证据时写“本次没有足够证据评价”。
- 需要改进：列出本次观察到的摩擦、能力缺口或可优化点；没有时写“本次未发现明确改进项”。
- Bug：列出可复现的异常、影响和可用绕行方案；没有时明确写“本次未发现 QCU Bug”。
```

Do not classify missing macOS permissions, login/CAPTCHA, user approval
requirements, or a documented unsupported tier as a QCU bug unless QCU itself
reports or handles that condition incorrectly. If an issue might be a bug but
is not yet reproducible, label it `疑似 Bug` and state what evidence is still
needed.
