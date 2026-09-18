# Desktop backends — 1.8

Desktop selects the current platform: macOS AX, Windows UIA, or an explicit
unimplemented Linux/HarmonyOS backend. It never defaults to macOS on Windows.
The common Layer/Observation/Action/LayerResult contracts are shared with web.

## Capability report

`doctor`, desktop session start, and native observations publish capability
reports. Preflight and target probes have different evidence:

| Field | Interpretation |
|---|---|
| `implemented` | Backend code exists |
| `dependencies_present` | Required import/dependency is present; not proof of live access |
| `session_accessible` | true/false/null: live access, failure, or not yet established |
| `can_read_controls` | true/false/null: controls read, unavailable, or empty/unexposed/unknown |
| `supported_actions` | Backend action vocabulary; each control still needs native support |
| `result_verification` | Has read-only postcondition implementation; individual checks may fail |
| `foreground_input_required` | false for UIA patterns; action-dependent for macOS |
| `available`, `reason` | Whether the current target was established as usable, and why not |

A root-only/empty tree can reflect provider exposure, privileges, transient
state, truncation or cross-process restrictions. Do not conclude that the app
has no controls. `read_errors`, `tree_truncated` and native properties preserve
those boundaries. Dependency installation is never the availability probe.

## macOS AX

Install `.[macos]`. Accessibility enables AX reading/actions; Screen Recording
is relevant to capture/OCR. `doctor` shows permission probes, with null for probe
errors. Input Monitoring checks listening access, not delivery of synthetic
input. Desktop start may prompt for permissions; the user must grant them.

```bash
qcu session start --context desktop
qcu observe --pid 12345 --window 'Fixture window' --compact
```

Explicit app/PID never silently falls back to the frontmost app. A requested
window must uniquely match. Zero matches reports `window_not_found`; multiple
matches reports `ambiguous_window` with candidates. The live PID/window binding
is inherited by later reads and actions; closing the window invalidates it.

Native refs carry a backend lifecycle, target and observation. Native handles
are never reconstructed after restart by path, ordinal or name. Before use, AX
checks live handle, PID, window membership and enabled state. Informational AX
text is kept in `raw_tree` even when it has no actionable ref.

AXPress and AXValue are preferred. After entering an AX call, ambiguous errors
stop; no repeat press, activation/repress, Apple Events action or keyboard paste
is used to resolve uncertainty. `fill` reads the exact value back. Generic AX
signal changes are UI evidence; use `verify` to confirm a requested result.
Explicit `double_click` retains its two-click meaning.

Every observe attempts to unlock the target's full AX tree by asserting
`AXEnhancedUserInterface` and `AXManualAccessibility` on the application
element (standard AX-client behavior; VoiceOver does the same). The outcome is
reported per-observation as `routing_meta.enhanced_ui`:

- `enabled` — set succeeded (`via` says which flag, or `already_true` when the
  set was rejected but the flag read back True, as newer macOS builds do).
- `already_set` — confirmed earlier in this backend lifetime (cached per pid).
- `failed` — both flags rejected and read-back shows the flag off; NOT cached,
  so the next observe retries. Treat a sparse tree as possibly unlock-related.
- `unavailable` — the AX call itself could not be made.

The unlock is effective for Catalyst/WebKit apps (verified on App Store: full
window content — search box, links, tab controls — becomes readable). It is
NOT sufficient for modern Chromium/Electron apps: real-machine probes against
Cursor and Qianwen show both flags accepted/already-true while the renderer
still exposes no window content at runtime. For those apps use the CDP route
(attach with `--remote-debugging-port` or relaunch with
`--force-renderer-accessibility`); AX alone cannot see their DOM.

Quartz keyboard/scroll/coordinate paths need a matching foreground target and
live window ownership. `--strict-background` rejects focus-stealing operations
and detects foreground drift; it is not background keyboard delivery.

## Screenshots, coordinates and OCR

A capture, grounding, action and verification share the same session/backend/
window or browser document. No browser is created by desktop fallback. Missing
capture, geometry, ownership or pattern support returns a specific unavailable
reason. An explicit layer override cannot bypass a conflicting target scope.

The macOS capture path uses the bound window ID, reports screen-point origin
and image scale, and rechecks the target around capture. It does not capture the
entire desktop in place of a missing window. The coordinate gate checks PID,
window identity, bounds and overlays, and **fails closed** when Quartz inspection
is missing. Negative screen origins are valid; coordinate spaces are explicit.

Local `_vision` OCR is a callable helper for visible window text, with the same
ownership checks and one dispatch. Pixel differences are `ui_changed`, not a
verified task result. Only explicit text conditions establish OCR outcome.
OCR fill is unsupported because focus and field-value identity are not proven.
General icon/template/VLM grounding and Windows capture are not implemented.

Apple Events is an explicit read-only compatibility observer in this preview.
Its positional control references and incomplete scope contract do not satisfy
the action guarantees. Its `act` returns unsupported/read-only before sending;
`--pid`/`--window` are rejected rather than ignored. Observe with AX for actions.

## Electron/CEF via the CDP bridge (`desktop_cdp`)

Real-machine probes (1.9): modern Chromium/Electron apps keep renderer
accessibility off at runtime — asserting `AXEnhancedUserInterface` /
`AXManualAccessibility` is accepted but the AX tree still exposes only the
menu bar (verified on Cursor and Qianwen). When the app was launched with
`--remote-debugging-port=<port>`, the CDP bridge attaches to that endpoint and
reuses the web engine, yielding the app's full DOM (controls, values, refs,
fill/click/verify) at web speed instead of screenshots.

```bash
qcu observe --layer desktop_cdp --pid 12345 [--window 'title filter'] --compact
qcu act '{"type":"fill","params":{"ref":"…","value":"…"}}'   # router stays on desktop_cdp
```

Guarantees and limits:

- Discovery probes only TCP ports the *bound pid* listens on, and each must
  answer `/json/version` with a debugger URL. No host-wide port scan; a
  missing endpoint reports `no_listening_ports`/`no_cdp_endpoint` plus a
  relaunch hint — never a substituted target.
- Page selection refuses to guess: a `--window` title filter must match
  exactly one page; zero/multiple matches list the available titles.
- The bridge never launches, relaunches, navigates or closes the user's app;
  `close()` drops only the local driver. A plain web observe/act detaches the
  binding explicitly before touching QCU's own browser.
- Follow-up actions stay on the bridge (router rule `desktop_cdp_bridge`);
  bridged observations persist as desktop context so refs and routing agree.
- Known web-engine caveat, not bridge-specific: on rich editors a `verify`
  condition may miss a successful fill — `inner_text` excludes live textarea
  values, and a ref tagged on a wrapper container reads no `value`. The fill's
  own locator read-back still confirms the write; treat `outcome=unknown`
  there as a verification gap, not a failed fill.
- Requires the daemon (default) so the attach survives across CLI calls.

## WKWebView via the embedded JS bridge (`desktop_jsbridge`)

Feasibility finding (macOS 26, 1.9): WKWebView content cannot be automated
from outside the process. AX shows only the `AXWebArea` shell; the
`AXEnhancedUserInterface`/`AXManualAccessibility` unlock does not reach the
DOM; and Apple's remote-inspection path has no programmatic client — no
`webinspectord` binary exists on this macOS, and Safari's Develop menu
requires private entitlements plus manual GUI steps.

For apps the owner controls, `examples/QCUWebViewBridge.swift` is a ~150-line
reference bridge: one line (`QCUWebViewBridge.shared.attach(webView)`) serves
a loopback-only, token-authenticated evaluate endpoint and publishes
`$QCU_BRIDGE_DIR/<pid>.json` (default `~/.qcu/bridges/`). QCU then drives the
webview's DOM with full semantics:

```bash
qcu observe --layer desktop_jsbridge --pid 12345 --compact
qcu act '{"type":"fill","params":{"ref":"…","value":"…","verify":{"kind":"value","ref":"…","equals":"…"}}}'
```

Guarantees and limits:

- Discovery trusts only a bridge file naming the bound pid, in the bridge
  directory, whose advertised port answers `/qcu/info`; each outcome
  (`pid_not_running`, `no_bridge_file` + embed hint, `bridge_file_invalid`,
  `bridge_not_responding`) is reported distinctly. No target substitution.
- The Swift side binds loopback only and requires the per-launch random token
  (`X-QCU-Token` header) for every evaluate; the bridge file is removed
  atexit so stale ports are never advertised.
- Actions (`click`/`fill`/`hover`) report `sent` only when the JS dispatched;
  a vanished element is `stale_ref`/`not_sent`; a JS dispatch exception is
  `outcome_unknown` with `retry_safe=false`. `verify` conditions are read back
  from the live DOM (`value`/`checked`/`selected`/`text`).
- Follow-up actions stay on the bridge (router rule `desktop_jsbridge_keep`).
- Sandboxed apps: their home is the container — set `QCU_BRIDGE_DIR` to a
  shared path (e.g. an app group) on both sides.
- Real-machine verification (probe app embedding the reference bridge):
  observe listed all DOM controls; fill verified its own value; a click was
  confirmed by its page-text result — all `outcome=verified`.

## Windows UIA minimum — Windows 真机未验证

Install `.[windows]`, use an interactive desktop, and select a PID/window. UIA
can resolve a supplied HWND directly; title/PID selection enumerates top-level
window metadata only, not the entire desktop UIA tree.

```powershell
qcu session start --context desktop
qcu observe --pid 12345 --window 0x123456 --compact
```

Bound identity includes process start time and image, PID, HWND, native runtime
ID and session. Traversal is bounded by depth (maximum 32), nodes (default 500,
maximum 2000 via Python observe), and time. It emits name, type, value, enabled,
focus/offscreen state, bounds, parent/child refs, runtime/native properties,
actual patterns and available actions. Foreign-process subtrees report skips.

| Action | Required pattern | Verification |
|---|---|---|
| `click` / backend `invoke` | InvokePattern | Explicit result text/state |
| `fill` | ValuePattern, writable | Exact value readback |
| `toggle` | TogglePattern | Explicit checked state; one cycle only |
| `select` | SelectionItemPattern | Selected=true readback |

Implementation uses **uiautomation** for direct COM pattern access. It was the
existing optional dependency and provides the four required patterns without
bringing pywinauto's higher-level input/click behavior into semantic execution.
Calls use the underlying pattern interfaces, never `Control.Click`, simulated
mouse defaults, `SendKeys`, or loops such as “toggle until desired state”.

A dedicated persistent MTA thread creates, owns, uses and releases UIA/COM
objects. Only JSON/dataclasses cross the worker boundary. A timed-out worker is
poisoned: new actions are refused, because the pending COM call may complete
later. Restart and observe again; no automatic replay. Disabled, stale,
ambiguous, readonly, inaccessible and missing-pattern cases are explicit.

Validation script: `py scripts/verify_windows_uia.py`. It starts a disposable
native fixture and an isolated QCU_HOME. Save its JSON output. Current macOS
contract/mock results do **not** establish live Windows provider, privilege,
threading, secure-desktop, remote-session or DPI behavior.

Official technical references:

- [Microsoft: UI Automation threading](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-threading)
- [Microsoft: control patterns](https://learn.microsoft.com/en-us/windows/win32/winauto/uiauto-controlpatternsoverview)
- [uiautomation project](https://github.com/yinkaisheng/Python-UIAutomation-for-Windows)

## Extending platforms

Register a platform selector using `qcu.platforms.register_desktop_backend`,
then register its Layer through `qcu.layers.runtime.register`. Missing platforms
return `backend_not_implemented`. Linux and HarmonyOS have registration points
only; neither has a full backend in this preview.
