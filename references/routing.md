# Routing and target binding — 1.8

The router remains a small priority rule chain. `qcu/router/rules.py` and
`qcu/platforms.py` are the source of truth; decisions are recorded in telemetry.

## Selection

| Context | Default |
|---|---|
| Web | `web_a11y`, attached to QCU's selected Chromium page |
| macOS desktop | `desktop_ax` |
| Windows desktop | `desktop_uia` |
| Linux desktop | `desktop_linux` (not implemented) |
| HarmonyOS desktop | `desktop_harmony` (not implemented) |

Explicit `--layer` requests are subject to session/target compatibility. An
explicit desktop filter cannot be ignored by the web backend. Native references
remain on the backend that issued them. A missing cached ref fails before
routing instead of becoming a visual/coordinate request.

Canvas/visual rules can select `screenshot_fallback`, but it must borrow the
same session's already bound target. It does not launch another browser or
switch to a different desktop transport. `--no-screenshot-fallback` rejects an
automatic visual action route; explicit visual requests still must satisfy
all ownership checks. An opaque embedded web view reports an access limitation,
not permission to recreate its URL in a different browser profile.

A failed/empty observation does not trigger an automatic second backend read.
AX permission failure is returned by AX. Windows uses UIA even when its import
or interactive-session access fails. Apple Events is only an explicit read-only
legacy observer. Dependencies, access and supported actions are separate facts.

## Actions

Ref validation happens before dispatch: lifecycle, target, observation and live
control identity must agree. Target errors require re-observation. A single
request is sent at most once (except an explicit double-click's two clicks).
After a transport exception or unconfirmed result, execution stops. It does not
switch transport or repeat the action. Only read-only postcondition polling is
repeated. Batches stop on failed or unknown outcomes and preserve attempt counts.

A router decision is not an authorization, an availability proof, or a result
verification. `LayerResult.dispatch_state` and `outcome` describe those execution
facts. `ok`, a changed tree and a successful task postcondition are distinct.

## Features and diagnostics

Features include context/platform, selected desktop backend, observed layer,
role counts, target membership, canvas/WebMCP evidence, action type/parameters,
and native capability diagnostics. `routing_meta.target` and `ref_scope` identify
what was observed; `capabilities` describes backend and live-target limitations.

The CLI returns `router_decision` and actual `layer_used`. Compact mode trims
routing alternatives; telemetry retains full decisions. It writes to
`$QCU_HOME/telemetry.jsonl` (default `~/.qcu`). Use `qcu stats --since 7d` for local
summaries. Telemetry success means backend call success; read the result's
postcondition evidence before claiming task completion.

## Extension

Register a platform name/backend with `register_desktop_backend`, register the
Layer constructor with `runtime.register`, and implement the common contracts
and conservative capabilities. Keep platform imports lazy. Linux/HarmonyOS
currently return explicit unsupported observations/actions. The interface does
not imply that a provider, OCR model, explorer graph or vision service exists.
