# API reference — `Observation` and `Action`

This document is the canonical contract for everything the LLM sends to and
receives from QCU. It mirrors the JSON Schemas emitted by
`qcu schema observation` and `qcu schema action`.

## Observation

The single object returned by `qcu observe` (and a field inside the router
decision that the LLM can inspect for debugging).

```jsonc
{
  "context": "web",                       // "web" | "desktop"
  "url_or_app": "https://example.com",    // URL for web, app name for desktop
  "title": "Example Domain",
  "elements": [                           // interactive elements with stable refs
    {
      "ref": "ref_42",                    // pass this back to `act`
      "role": "button",                   // normalized: button, link, edit, ...
      "name": "Sign in",
      "value": null,
      "enabled": true,
      "focused": false,
      "bounds": { "x": 100, "y": 200, "width": 80, "height": 30 },
      "backend_id": "17",                 // opaque; CDP backendDOMNodeId or AX id
      "properties": { "raw_role": "button", "depth": 2 }
    }
  ],
  "routing_meta": { ... },                // why this layer was chosen
  "raw_tree": "[ref_1] button 'OK'\n...", // LLM-friendly indented tree
  "screenshot_path": null,                // set only when screenshot layer ran
  "tools": [],                            // WebMCP tools the page exposed
  "elapsed_ms": 23.4,
  "router_decision": { ... }              // rule chain result
}
```

### Normalized roles

A flat, opinionated vocab shared across ARIA, macOS AX, and Windows UIA.
Anything we can't map becomes `"other"` and the original role is preserved
in `properties.raw_role`.

```
button, link, edit, password, searchbox, checkbox, radio, switch,
combobox, listbox, option, list, list_item, tree, tree_item, menu,
menu_item, menu_bar, tab, tab_item, tab_panel, dialog, alert, tooltip,
window, pane, image, slider, progress, spinbutton, image, video, audio,
canvas, heading, separator, group, application, document, form, region,
table, row, cell, columnheader, rowheader, navigation, main, banner,
contentinfo, complementary, text
```

## Action

The single object passed to `qcu act`. Always an object with `type` and
optional `params`.

| type | params | example |
|---|---|---|
| `click` | `{ref}` or `{x, y}` | `{"type":"click","params":{"ref":"ref_42"}}` |
| `double_click` | same as click | `{"type":"double_click","params":{"ref":"ref_42"}}` |
| `hover` | same as click | `{"type":"hover","params":{"ref":"ref_42"}}` |
| `fill` | `{ref, text}` | `{"type":"fill","params":{"ref":"ref_17","text":"hello"}}` |
| `type` | `{text}` (types into focused element) | `{"type":"type","params":{"text":"abc"}}` |
| `press_key` | `{key}` — single (Enter/Tab/Esc) or **combo** (Cmd+Q, Shift+Tab, Cmd+Shift+3) | `{"type":"press_key","params":{"key":"Cmd+Q"}}` |
| `right_click` | `{ref}` or `{x, y}` — desktop context menu | `{"type":"right_click","params":{"ref":"ref_5"}}` |
| `drag` | `{ref_from, ref_to}` \| `{from:{x,y}, to:{x,y}}` \| `{x1,y1,x2,y2}` (+ optional `duration`, `steps`) — press-drag-release along an interpolated path | `{"type":"drag","params":{"ref_from":"ref_1","ref_to":"ref_9","duration":0.4}}` |
| `scroll` | `{dx, dy}` | `{"type":"scroll","params":{"dx":0,"dy":300}}` |
| `launch_app` | `{app}` — open/activate an app by name (desktop; works without AX grant) | `{"type":"launch_app","params":{"app":"TextEdit"}}` |
| `activate_app` | `{app}` — focus a running app (desktop) | `{"type":"activate_app","params":{"app":"Notes"}}` |
| `navigate` | `{url}` | `{"type":"navigate","params":{"url":"https://example.com"}}` |
| `go_back` / `go_forward` | `{}` — browser history (bfcache) / desktop Cmd+[/] | `{"type":"go_back"}` |
| `wait` | `{ms}` | `{"type":"wait","params":{"ms":500}}` |
| `screenshot` | `{path?}` | `{"type":"screenshot","params":{"path":"/tmp/p.png"}}` |
| `webmcp_call` | `{tool, args}` | `{"type":"webmcp_call","params":{"tool":"submit","args":{}}}` |

## LayerResult (response to act)

```jsonc
{
  "ok": true,                             // or false
  "layer": "web_a11y",                    // which layer ran
  "message": "click ref=obs_1:ref_42 (via locator)",
  "data": {                               // layer-specific extras
    // press_key / go_back / go_forward carry effect verification:
    "dispatched": true,                   // event was sent
    "effect_verified": false,             // did the page actually change?
    "no_effect": true,                    // dispatched but nothing moved
    "before_url": "...", "after_url": "...",
    "reason": "ambiguous"                 // locator failure classification, if any
  }
}
```

`press_key` no longer reports success purely because the key event was
dispatched: navigation/typing chords now require an observable URL/history/
document change, otherwise `effect_verified: false`. This is what surfaced the
"three keyboard shortcuts that never went back" incident.

### `data.verification` block (desktop_ax)

Every desktop action that can have an observable effect now attaches a
`verification` object to `data`, so callers can tell "dispatched" from
"actually happened":

```jsonc
"data": {
  "verification": {
    "verified": "yes",   // "yes" | "no" | "n/a" | "skipped"
    "signal": "value_set", // which AX signal channel was watched
    "matched_at_ms": 52.0, // time-to-match; null when verified=no
    "samples": 2,          // how many polls were taken
    "verify_diagnostics": { "value_after": "..." }  // free-form evidence
  }
}
```

- **`yes`** — an AX-state signal matched within the per-action window.
- **`no`** — the window elapsed with zero signal (likely silent no-op: a
  stub-noop Electron target, a disabled menu item, an `open -a` that
  spawned a process with no window). `ok` is still `true` (the action WAS
  dispatched); the caller decides whether to retry.
- **`n/a`** — the action type isn't amenable to verification (e.g. plain
  `click` on a button with no AX state, or a coordinate-only `drag`).
- Applies to: `click`, `fill`, `launch_app`, `activate_app`, `press_key`
  (state-changing chords only), `drag` (when the source ref is AX-resolvable).

## RoutingDecision (always returned, for transparency)

```jsonc
{
  "layer": "web_a11y",                    // chosen layer
  "reason": "Web context; a11y tree available — fastest path.",
  "priority": 70,                         // rule priority (higher = earlier)
  "rule_name": "web_a11y",                // rule identifier
  "alternatives": [                       // every other rule + its match status
    { "rule": "canvas_target", "layer": "screenshot_fallback", "matched": false, "priority": 100, "reason": "..." },
    { "rule": "visual_confirm", "layer": "screenshot_fallback", "matched": false, "priority": 95, "reason": "..." }
  ],
  "features": { ... }                     // feature dict the classifier saw
}
```

## CLI flags (cheat sheet)

```
qcu session start [--context web|desktop|auto] [--headed|--headless]
qcu session status
qcu session end [--purge-profile]            # stops the browser daemon
qcu browser status                           # daemon health (port/pid/alive)
qcu browser stop                             # force-stop the daemon
qcu observe [--max-depth N] [--layer NAME]
            [--full-text] [--text-limit N] [--tail]
            [--limit N] [--offset N] [--compact]
            [--role link] [--name substring]
qcu find    [--role link] [--name substring] [--limit N] [--offset N] [--compact]
qcu inspect <ref> [--compact]
qcu act <JSON> [--layer NAME]
qcu route <features.json | ->               # stdin if "-"
qcu stats [--since 7d] [--path PATH]
qcu schema observation
qcu schema action
qcu doctor                                   # permissions + install/version/drift
```

### Observe options

`--max-depth` bounds the CDP accessibility tree depth. A shallow request that
returns a large tree but almost no interactive refs triggers an **adaptive
deepen** (HN's front page: depth 6 → 14) so main content isn't silently hidden.
Every truncation surfaces as `routing_meta.tree_truncated`,
`routing_meta.text_dropped`, `routing_meta.structured_dropped`,
`routing_meta.geom_failed`, plus `deepest_returned_depth` so the LLM can tell
when a tree is partial.

`--full-text` lifts the text cap (read a long comment verbatim); `--tail`
returns only the last `--text-limit` text lines (the most recent comment);
`--role`/`--name` filter; `--limit`/`--offset` paginate; `--compact` drops
empty fields. `qcu find` and `qcu inspect` reuse the **persisted last real
observation** without re-snapshotting the page.

Refs are now generation-prefixed (e.g. `obs_12:ref_3`). After any navigation
or DOM replacement the session's `observation_generation` advances, so a stale
`obs_N` token is rejected with `data.reason: "stale_ref"` rather than replayed
against a fresh page element.

`--layer NAME` bypasses the router for that single call. Names:
`web_a11y`, `webmcp`, `desktop_ax`, `desktop_uia`, `screenshot_fallback`.

## Browser daemon lifecycle

The web layer does NOT launch a browser per command. It runs one **detached,
long-lived Chromium daemon** and every `qcu` command attaches to it via CDP.

| step | what happens |
|---|---|
| first web command | a detached Chromium is started with `--remote-debugging-port=<p> --user-data-dir=<ud> --headless=new`; `{browser_pid, browser_debug_port}` is written to `session.json` |
| every later command | `playwright.chromium.connect_over_cdp("http://127.0.0.1:<p>")` attaches — no new process, no DOM reset |
| `qcu session end` / `qcu browser stop` | the daemon pid is sent SIGTERM (→ SIGKILL on timeout); on Windows `taskkill /T /F` |

Why this matters: because attaching doesn't reload the page, **unsubmitted
form input, focus, scroll position, and the injected `data-llm-ref` attributes
all survive between `qcu` calls**. A `fill` in one process and a `press_key`
in the next behave as if they ran in one process.

Crash recovery: if the daemon died (port unresponsive), the next command
detects the dead `browser_pid`, removes a stale `SingletonLock`, and relaunches
automatically — no manual cleanup needed.

Environment variables:
- `QCU_HEADLESS=0` — show the browser window (default `1`, headless).
- `QCU_BROWSER_CHANNEL=chrome|msedge` — use a system Chrome/Edge instead of the
  Playwright-bundled Chromium.
- `QCU_USER_DATA_DIR=<path>` — override the profile. Must be a non-default
  dir: Chrome 136+ ignores `--remote-debugging-port` for the default profile.

Security: the CDP endpoint binds to loopback only and has no authentication
(it grants full browser control). Never forward the port to a remote host.