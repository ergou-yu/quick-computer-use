# Changelog

All notable changes to this project are documented in this file. Versions
follow [Semantic Versioning](https://semver.org/).

## [1.9] — 2026-09-18

Harden the AX enhanced-UI unlock on macOS, and add the Electron/CEF CDP bridge.

- `desktop_ax` now checks the `AXEnhancedUserInterface` set result instead of
  swallowing it: a failed set is no longer cached, is retried on the next
  observe, and is reported per-observation as `routing_meta.enhanced_ui`.
- Fall back to `AXManualAccessibility` when the enhanced set is rejected
  (Chromium/Electron honor both), and disprove a rejected set by reading the
  attribute back — newer macOS builds return kAXErrorNotImplemented for the
  set while the flag is already True.
- Set the flags before reading windows/children so the first observe of a
  Catalyst/WebKit app already benefits.
- Real-machine verification: App Store (Catalyst) exposes full window content;
  Cursor and Qianwen (Electron) accept the flags but keep renderer a11y off at
  runtime — documented as a known limit, which the CDP bridge now covers.
- New `desktop_cdp` layer: attach to an Electron/CEF app launched with
  `--remote-debugging-port` and drive its full DOM through the web engine
  (`qcu observe --layer desktop_cdp --pid <pid>`). Discovery probes only the
  bound pid's own listening ports; page selection refuses ambiguous guesses;
  the app is never launched, navigated or closed; follow-up actions stay on
  the bridge via the `desktop_cdp_bridge` router rule; plain web requests
  detach the binding explicitly.
- Real-machine verification (Cherry Studio, disposable profile): full DOM
  observed where AX showed menu bar only; fill dispatched and confirmed by
  locator read-back; verification mismatches on rich editors reported
  honestly as `outcome=unknown`.
- New `desktop_jsbridge` layer plus `examples/QCUWebViewBridge.swift`: WKWebView
  content is not externally automatable (verified on macOS 26 — no
  webinspectord; Safari remote inspection needs private entitlements and
  manual GUI). Apps the owner controls embed the one-line bridge; QCU then
  observes and drives the DOM over a loopback-only, token-authenticated
  endpoint with honest dispatch/verification semantics.
- Real-machine verification (probe app with the reference bridge): all DOM
  controls observed; fill reached `outcome=verified` on its value; a click
  was verified via its page-text result.

## [1.8] — 2026-09-16

Control reliability and platform foundation. **Windows real device unverified.**

- Scope AX observations to a unique requested window; missing/ambiguous targets
  fail and later reads/actions retain the same binding.
- Share lifecycle/target/observation reference scopes across AX, UIA and web;
  reject stale/native restart refs and remove path/name/coordinate relocation.
- Add `dispatch_state` and `outcome`; only explicit postconditions/readback
  establish a requested result. Stop on ambiguous transport/verification;
  never replay a single click or fill. Batches stop on unknown outcomes too.
- Add value/checked/selected/text verification. Preserve UI-change diagnostics
  separately from task-result evidence.
- Bind capture, grounding, coordinates and verification to one session/target;
  fail closed when window ownership cannot be inspected.
- Add platform registry/capabilities and minimal Windows UIA Invoke, Value,
  Toggle, SelectionItem patterns on a persistent COM worker. Isolate platform
  dependencies and report Linux/HarmonyOS as not implemented.
- Make the positional Apple Events compatibility observer read-only; OCR fill
  is unsupported until focused field identity/value can be verified.
- Add mock contracts, isolated Chromium regressions, disposable macOS/Windows
  native validation scripts and migration/install documentation.
- Add performance benchmarks beyond the web form script: a vision-loop local
  baseline (Quartz capture + Apple Vision OCR), a raw-Playwright floor, and a
  macOS desktop AX calculator run (`scripts/benchmark_vision_baseline.py`,
  `scripts/benchmark_playwright_floor.py`, `scripts/benchmark_desktop_ax.py`).
- Add `TEST-REPORT.md` — the 2026-09-16 release test report covering the
  regression suite and the web/desktop/vision performance measurements.

Migration: restart the task daemon, observe new opaque refs, inspect top-level
outcome, and provide final batch action postconditions. `strict_ref` remains
accepted; web ref actions now always refuse cached-coordinate fallback. Move
Apple Events actions to newly observed AX refs. This contract tightening was
consolidated into the 1.8 release; Windows is not advertised as fully
supported. Historical test counts below remain historical; this round's
results are in TEST-REPORT.md.

## [1.7.1] — 2026-09-08

- Verify native button effects in the target element's AXWindow, rather than
  the first application window (which can be an unrelated auxiliary window).
- Stop after a dispatched AX action with an unconfirmed effect or send error.
  Return `ok:false`, `data.reason: outcome_unknown`, and `retry_safe:false`;
  do not activate and re-press or replay through Apple Events/coordinates.
- Capture coordinate-fallback state before the event. Preserve uncertain
  outcomes when a pre-action snapshot is missing or verification is unavailable.
- Start the verification polling budget after dispatch, so slow AX calls cannot
  consume the budget and prevent every post-action observation.
- Add multi-window and at-most-one-dispatch regressions, including explicit
  double-clicks and ambiguous transport errors.

## [1.7] — 2026-09-06

- Add bounded `batch` form groups with one optional final observation, strict
  web refs, value readback, and stop-on-failure results.
- Remove unconditional network-idle waiting; add explicit target/readiness waits.
- Serialize daemon UI work on one persistent thread, keep health checks separate,
  preserve stderr, and prevent local replay after an ambiguous RPC outcome.
- Stop the Python daemon on session end; avoid orphaning a daemon before a
  logical session exists. Compact output retains disabled/focused state.
- Return native AXStaticText values and bounded result/status text. Verify app
  launches by exact bundle path and wait for process registration as well as windows.
- Integrate the 1.6.1 distribution's pre-retry AX snapshot fix into the active source.
- Rewrite the skill around current capabilities, scoped observations, short
  action groups and observable outcomes. Remove unsupported universal speed claims.
- Add unit/RPC/browser regressions and a loopback-only reproducible benchmark.

## [1.6.1] — 2026-09-01

- The distribution wheel captured an AX snapshot before the foreground retry,
  preventing immediate effects from being missed and redundantly actuated.
  This fix had not reached the active editable source; 1.7 reconciles the copies.

## [1.6] — 2026-08-21

### Fixed
- **`qcu observe` crashed on numeric input values.** CDP's a11y tree types
  `value.value` by the DOM property, so `<input type=number/range>` yields an
  **int** and `aria-checked` a **bool**; a raw int into `clean_text`'s
  `re.sub` raised `TypeError` and killed the whole observe (any page with a
  quantity field). `clean_text` now coerces non-strings (bool → "true"/"false",
  0 → "0" — a legitimate slider value, previously collapsed to "").
- **SwiftUI buttons were anonymous.** macOS SwiftUI controls keep their
  accessible label in `AXDescription`, not `AXTitle`; QCU read only AXTitle,
  so Calculator's keypad (and most modern Apple apps) surfaced 54 unnamed
  buttons the LLM could only guess by list position. The walk now falls back
  to AXDescription for interactive elements with an empty title (fetched
  lazily, so the role-first traversal stays fast).
- **Verify could never match fast effects (before-snapshot ordering).**
  `_click` fired AXPress and only THEN captured the "before" snapshot inside
  `_verify_action` — for effects that land within the press round-trip
  (Calculator keypad → display) before==after and verify always returned no,
  cascading into 3+s Apple Events fallbacks for clicks that HAD worked
  (4.5–8.8 s wall time, `ok=false`). The snapshot is now taken before the
  press and passed through. Measured: verified digit click now **64 ms**
  (foreground) / **84 ms** (background, activate-retry path), 8/8 verified.
- **Daemon `verify` used a stale app pid.** `_verify_action` resolved the app
  element by NAME via NSWorkspace — whose process list is cached forever in a
  long-lived daemon (after an app quit+relaunch it returns the dead pid;
  AXWindows on it is empty, so window-scope verify signals silently never
  fired). It now derives the pid from the target element itself via
  `AXUIElementGetPid` (live AX runtime, cannot be stale).
- **Daemon observed a dead pid after app relaunch (0 elements).** Same cached
  NSWorkspace list: after Calculator quit+reopen, `observe --app Calculator`
  kept resolving the dead pid's proxy and returned 0 elements while
  `observe --pid <new>` worked. Every name-matched candidate is now
  health-probed (AXWindows non-empty); when all candidates are dead the live
  pid is resolved from the Quartz window list. Same-daemon observe across a
  quit+reopen cycle: 276 → 0 (broken) → **276 (fixed)**.
- **`observe --app/--pid/--window` without a session routed to web.** The
  auto-start default (`context=web`) ignored explicit desktop scoping, so
  "look at Calculator" with no prior `session start` returned a blank
  about:blank web observation. Desktop scoping now implies `context=desktop`.
- **Background no-op clicks are retried after activation.** Apps that ignore
  AXPress while backgrounded (Calculator keypad, probe-verified: err=0, zero
  effect) now get one focused retry: resolve the owning app (element pid →
  NSWorkspace), activate, re-press, re-verify with a widened 2.5 s window —
  before falling through to the slow coordinate/Apple Events paths.

## [1.5] — 2026-08-21

### Changed
- Version bump only. No code changes in this release; the Python source,
  tests, and skill docs are identical to 1.4.

## [1.4] — 2026-08-10

### Fixed
- **Self-healing against hostile `PYTHONPATH`.** When a third-party tool
  (e.g. OpenClaw) injects a `PYTHONPATH` that bundles a pyobjc compiled for a
  different Python ABI (3.11 vendored libs vs. a 3.13 interpreter),
  `import objc` fails and the entire macOS Accessibility stack becomes
  unimportable — silently degrading QCU from the fast `desktop_ax` layer
  (~250 ms, hundreds of elements) to the `desktop_appleevents` fallback
  (~1.5 s, only 3 window buttons) AND making `qcu doctor` unable to probe
  permissions. QCU now detects and strips the incompatible path entry at
  package import time, before any pyobjc import — self-healing, no reboot
  or env editing required. Covers all entry points: `python3 -m qcu`, the
  shim, and the detached daemon.
- **Transparent overlay occlusion** (the "Chrome reported frontmost but
  Mirroria covered it" bug). The click-safety gate previously skipped every
  non-zero-window-layer window, so a floating overlay (Mirroria, 悬浮球,
  notification) sitting above a target Chrome window was invisible to the
  owner check — clicks passed the check and leaked to the overlay. The gate
  now walks higher-stacked candidates and aborts with `occluded_by_overlay`
  when a different-app, non-transparent overlay covers the point. Also
  fixed an `alpha` truthiness bug (`0.0 or 1.0` → 1.0) that turned
  click-through transparent overlays into opaque ones.
- **Screenshot captured a stale example.com page** (the silent session
  "reset"). A screenshot path no longer triggers `_maybe_rewind_to_last_url`,
  so it captures the page AS-IS instead of silently re-navigating to a stale
  `session.current_url`. Sentinel/demo URLs (`example.com`, `about:blank`,
  `example.org`) are additionally never replayed onto a fresh daemon.
  Rewind failures now surface on stderr instead of being swallowed.
- Observation-blocker remediation (the Kimi "無法讀取 UI" report): when a
  desktop observe/act degrades (AX not trusted / Automation not granted /
  pyobjc missing), QCU now emits a loud bilingual remediation in both
  `raw_tree` (the reliable channel — travels through daemon RPC JSON
  unchanged) and stderr — instead of returning a near-empty result with the
  cause buried in `routing_meta.reason`. Before this fix, agents saw an
  empty element list with no actionable guidance and abandoned desktop
  automation for the terminal.

### Added
- **Browser detection → T3 takeover signal.** Stock Chrome/Safari/Edge/Arc
  does not expose `AXWebArea` without `AXManualAccessibility`, so the router
  never escalated a browser window to the T3 web-takeover hint — even though
  its DOM is just as opaque. A new `has_web_app` feature (populated by the
  AppleScript tab probe) fires `desktop_webview_blind` off `web_app` alone,
  surfacing the "switch to web context" recommendation in the router reason,
  `routing_meta.web_view`, AND a stderr line — three channels instead of a
  raw_tree comment.
- **`wait` action accepts `seconds`/`s`.** LLMs naturally emit
  `{"type":"wait","params":{"seconds":2}}`; previously only `ms` was read
  and the default was 100 ms, so a "2 second" wait actually slept ~100 ms.
  Now `seconds`/`s` resolve correctly (shared `wait_ms` helper in
  `common/normalize`); `ms` still takes precedence for explicit callers.
- **App-launch disambiguation.** Launching by name when multiple `.app`
  copies exist (an installed copy plus an Xcode DerivedData / `.build` /
  staging clone) previously took the first `mdfind` hit — unspecified order,
  frequently the wrong one. Candidates are now filtered (build noise
  removed) and sorted with a `/Applications` preference; an absolute `.app`
  path short-circuits Spotlight entirely. Multiple surviving candidates
  warn on stderr.

### Changed
- **Retina / mixed-DPI scaling.** The OCR→click conversion hardcoded
  `/2` and `*2`, correct only on a 2× Retina display — silently
  mis-clicking on a non-Retina external monitor. A new
  `_backing_scale_for_bounds` helper probes the real
  `NSScreen.backingScaleFactor()` for the screen hosting the window
  (fallback 2.0). The AX→CGEvent path was already point-space-correct and
  is unchanged.
- Install/test docs (`test_strict_background.py`, `test_observe_focus_and_noscreenshot.py`)
  stub `Quartz.CoreGraphics` with a more complete fake symbol set, fixing
  spurious cross-test failures when the full suite runs.
- SKILL.md gained new Common Mistakes: app-name ambiguity (pass absolute
  path), multi-monitor negative-coordinate clicks (re-observe to refresh
  bounds), headed-mode navigation stalls (interstitials), and
  `trusted: false` ≠ empty UI (check permissions first).

## [1.3] — 2026-08-06
- Resident Python CLI daemon (warm layer singletons across calls).
- `qcu browser status/stop` and `qcu daemon status/stop` diagnostics.
- Detached Chromium daemon with CDP attach (state preserved across calls).

## [1.2] — 2026-08-05
- Desktop action verification (AX state-diff, no silent no-ops).
- `--strict-background` and `--no-screenshot-fallback` session flags.
- `qcu observe --app/--pid/--window` scoping.

## [1.1] — 2026-08-03
- macOS Accessibility (AX) desktop layer (`desktop_ax`).
- Apple Events fallback layer (`desktop_appleevents`).

## [1.0] — 2026-07-31
- Initial public release: web `web_a11y` layer, router, observation/action schema.
