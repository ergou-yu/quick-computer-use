# QCU API — 1.8

## Target and observation

`qcu observe` returns `context`, `url_or_app`, `title`, `elements`, `raw_tree`,
`routing_meta`, optional `screenshot_path`/`tools`, and CLI routing/timing fields.

Each Element has an opaque `ref`, normalized `role`, optional `name`/`value`,
`enabled`, `focused`, `bounds`, `backend_id`, and backend `properties`.
Geometry uses web CSS pixels, macOS screen points, or Windows UIA screen pixels;
never transfer coordinates between these spaces.

`routing_meta.target` identifies the observed scope:

- Web: browser port, CDP tab identity and page lifetime, URL/title.
- AX: PID/app and bound window identity/title; native handles remain in memory.
- UIA: PID/process creation time/image, HWND, runtime identity and session ID.

`routing_meta.ref_scope` reports backend, lifecycle, target and observation
generation. Public refs extend the original web observation generation with
backend lifetime and target identity:

```text
<backend>:<lifecycle>:<target-digest>:obs_<generation>:ref_<index>
```

Treat this entire string as opaque. `find` and `inspect` search the cached
observation; they do not refresh it. A reference becomes invalid after a new
observation, backend restart/close, target change, or loss of the exact native
handle/DOM annotation. Disabled or moved-to-another-window controls are refused.
Invalid refs are not rebound by name, path, reused numeric ID or former geometry.

`--pid`, `--app`, `--window` are desktop observation filters. AX matches window
names; UIA accepts a title substring or decimal/hex HWND. Missing and multiple
matches return errors; candidates are included for ambiguity. Subsequent native
observations/actions inherit the binding. Conflicting target parameters fail.
A screenshot fallback cannot change session context or select a different app.

## Actions

```json
{"type":"click","params":{"ref":"<returned-ref>"}}
```

| Action | Parameters / coverage |
|---|---|
| `click` | `{ref}`; semantic AXPress / UIA Invoke / web locator |
| `fill` | `{ref,text}`; AXValue / UIA Value / web locator; reads value back |
| `toggle` | `{ref}`; UIA TogglePattern, once |
| `select` | `{ref}`; UIA SelectionItem.Select, verifies selected |
| `double_click` | Web/AX only; explicit two-click semantics |
| `hover`, `right_click` | Backend dependent; inspect capabilities |
| `drag` | `{ref_from,ref_to}` or explicit `{x1,y1,x2,y2,duration?,steps?}`; live geometry/safety required |
| `type`, `press_key` | `{text}` / `{key}`; web or macOS; foreground native target must match |
| `scroll` | `{dx,dy}`; web or macOS |
| `navigate`, `go_back`, `go_forward` | Web navigation; `{url}` for navigate; no automatic retry |
| `launch_app`, `activate_app` | macOS `{app}`; focus constraints apply |
| `screenshot` | `{path?}`; captures bound target, does not prove an action's result |
| `wait` | `{ms}`; backend dependent |
| `webmcp_call` | `{tool,args}`; only actually discovered page capabilities |

Explicit coordinates `{x,y}` are supported for applicable web/macOS operations.
They must pass the selected target's checks. `strict_ref` is accepted for old
callers; all web ref actions now refuse cached-coordinate fallback.
UIA supports only patterns actually exposed by the element. Type names alone
never authorize Invoke/Value/Toggle/SelectionItem; missing patterns return
`reason: unsupported`, `dispatch_state: not_sent`.

## Requested postconditions

Optional `params.verify` is one small condition, not a workflow language:

```json
{"kind":"value","ref":"<field-ref>","equals":"Example"}
{"kind":"checked","ref":"<toggle-ref>","equals":true}
{"kind":"selected","ref":"<item-ref>","equals":true}
{"kind":"text","contains":"Saved","timeout_ms":1500}
```

- `value`, `checked`, `selected` require a current ref from the bound target.
- `text` can optionally target a ref; without it, inspect text on the same
  browser page/window. AX/UIA text reads are bounded, so a missing match is not
  proof that the application has no such text.
- Text/value accept one string `equals` or `contains`; states require boolean
  `equals`. Missing state is unknown, never implicitly false.
- `timeout_ms`: 0–10000, default 1000; UIA caps polling at 5000. Polling repeats
  read-only checks, never the action. Invalid conditions fail before dispatch.
- Successful `fill` supplies its own exact value readback; UIA `select` supplies
  selected=true. Toggle cycles once; give the expected state explicitly.
- Generic OCR supports text evidence only. OCR fill/focus/value verification,
  Windows capture/keyboard and general visual grounding are unavailable.

## LayerResult

```json
{
  "ok": true,
  "layer": "web_a11y",
  "message": "click sent",
  "dispatch_state": "sent",
  "outcome": "unknown",
  "data": {"dispatched": true, "retry_safe": false}
}
```

| Field/value | Meaning |
|---|---|
| `dispatch_state: not_sent` | Rejected before sending the UI action |
| `dispatch_state: sent` | Backend accepted/completed the dispatch call |
| `dispatch_state: unknown` | The call may have sent the action; reply/error is ambiguous |
| `outcome: unknown` | Requested result has not been confirmed, including no condition supplied |
| `outcome: verified` | Exact requested postcondition was observed |

`ok` retains the backend's success/failure signal for compatibility. It **does
not imply task completion**. A field-value check proves the field value, not
that a form was persisted. Generic AX tree changes, URL changes and screenshot
pixel differences are UI evidence only (`effect_verified` / `ui_changed` or
legacy `data.verification.verified: yes/no/n/a/skipped`). Only top-level
`outcome: verified` means a requested condition passed.

`data.dispatched` is a legacy mirror: true, false, or null. Sent/unknown results
carry `retry_safe:false`. `reason:outcome_unknown` reports failed/ambiguous
verification or transport; it never causes automatic re-click, submit, paste,
navigation or alternate-transport execution. Read state before planning further
work. CLI/RPC lost responses use the same top-level states.

## Batch

`qcu batch '<JSON array>' [--observe] [--compact]` accepts 1–32 actions. Only
fills may precede the final action. Cached ref membership is checked before
execution; the backend still validates live identity before each step.

The batch stops on failure **or unknown outcome**, even when `ok:true` means the
last click was sent. Add a meaningful condition to the final action to confirm
its result. Completed actions are not rolled back or replayed.
`executed` counts attempted actions including the stopped step; `stopped_at` is
zero-based. `results` preserves each result. `--observe` reads fresh state after
failure too; it does not retroactively mark an unverified step as completed.

## Commands and readiness

```text
qcu session start --context web|desktop [--headed|--headless]
                  [--strict-background] [--no-screenshot-fallback]
qcu session status
qcu session end [--purge-profile]
qcu observe [--app NAME] [--pid PID] [--window TITLE_OR_HWND]
            [--layer NAME] [--max-depth N] [--compact]
            [--role ROLE] [--name SUBSTRING] [--limit N] [--offset N]
            [--full-text] [--text-limit N] [--tail]
            [--wait-until domcontentloaded|networkidle]
            [--wait-for KNOWN_CSS_SELECTOR] [--timeout-ms N]
qcu find [--role ROLE] [--name TEXT] [--compact]
qcu inspect REF [--compact]
qcu act JSON [--layer NAME]
qcu batch JSON_ARRAY [--layer NAME] [--observe] [--compact]
qcu daemon status|stop
qcu browser status|stop
qcu doctor
qcu route FEATURES_JSON_FILE
qcu schema observation|action
qcu stats [--since 7d]
```

Web observes default to `domcontentloaded`, never an implicit `networkidle`
wait. `--wait-for` waits for a visible known selector (timeout 1–60000 ms).
Readiness is not a business postcondition. Pagination/truncation diagnostics
explain partial reads. An empty tree is an exposure/access diagnostic, not a
conclusion that the app has no controls.

The Python daemon serializes UI work and retains layer handles. Chromium uses a
separate persistent QCU profile. Closing a local layer drops that connection;
`session end` stops the task's daemons. Purge only disposable profiles. A Windows
UIA worker timeout blocks new work in that worker until restart/re-observation.

## Migration from 1.7.1

1. Stop this task's daemon after installing; old imported code is not hot patched.
2. Start the intended session and observe again. Do not transform old numeric or
   `obs_N:ref_M` refs into new tokens; all old refs require fresh observation.
3. Replace `ok`-only business success checks with `outcome == verified` and an
   explicit condition. Unknown batch results now stop even after a sent click.
4. Remove assumptions that a failed web locator will use cached coordinates or
   that a missing desktop window will expand to the whole application.
5. Migrate Apple Events actions to newly observed AX controls. Apple Events is
   an explicit read-only legacy observer; pid/window scoping is unsupported.
6. Query platform/target capabilities. Windows is a minimal preview, not full
   desktop parity; Linux/HarmonyOS are explicit unimplemented registrations.
