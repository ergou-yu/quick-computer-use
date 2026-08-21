# Changelog

All notable changes to this project are documented in this file. Versions
follow [Semantic Versioning](https://semver.org/).

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
