# QCU — Quick Computer Use

**1.10.0.** QCU controls an explicitly selected browser page or
desktop window with accessibility references. This revision binds references to
a backend lifetime, target and observation; separates dispatch from verified
results; and supports Windows UI Automation. Real Windows 10 native and CLI
fixtures are verified; persistent DOM/CDP, compact observations, cached queries
and bounded batches retain their fast paths.

Linux now provides an AT-SPI2 semantic backend and an optional X11 screenshot/
XTEST input fallback. See [Linux setup and limits](references/linux.md).
The Ubuntu GTK/Xvfb workflow passed 771 regression tests and a separate
25-command live fixture; virtual desktop coverage is distinct from physical
machine and Wayland compositor validation.

## Install

Python 3.11+:

```bash
python -m pip install -e '.[dev]'        # web / development
python -m playwright install chromium   # web only
python -m pip install -e '.[macos,dev]'  # macOS AX
```

On Windows PowerShell:

```powershell
py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[windows,dev]"
& .\scripts\qcu.ps1 --version
& .\.venv\Scripts\python.exe scripts\verify_windows_uia.py --output uia-result.json
```

Platform extras use environment markers. Web/macOS installations do not import
or install Windows UIA dependencies. See [INSTALL.md](INSTALL.md).

## Target → observe → act → verify

```bash
qcu session start --context desktop
qcu observe --pid 12345 --window 'Fixture window' --compact
# Copy opaque refs from this observation. Example placeholder:
qcu act '{"type":"click","params":{"ref":"<returned-ref>","verify":{"kind":"text","equals":"Saved 1"}}}'
qcu session end
```

For web use `session start --context web`, navigate, then observe. QCU Chromium
has its own profile; it does not inherit the user's open authenticated tab.
Preserve the selected target. Tests must use an independent `QCU_HOME`.

- Explicit missing or ambiguous windows fail with diagnostics/candidates.
- `routing_meta.target` identifies the app/window or browser tab.
- Re-observe after a backend restart, target change or invalid reference. Old
  numeric and `obs_N:ref_M` tokens are not upgraded into new control identities.
- Ref actions never become cached-coordinate actions. Coordinate/OCR paths must
  establish the same target; unavailable inspection stops them.
- A successful call or changed interface is distinct from the requested result.
  Only `outcome: verified` confirms the specified postcondition.
- `batch` permits 1–32 actions, fills before one final action, and stops on
  failure **or unknown outcome**. `executed` counts attempted steps. Provide a
  `verify` condition for the final click if batch success requires its result.

## Platform coverage

| Platform | Backend | Scope |
|---|---|---|
| macOS | `desktop_ax` | AX controls/actions, bounded window capture and safety checks |
| Windows | `desktop_uia` | Semantic patterns; native HWND PNG screenshots and guarded mouse/keyboard message fallback; real WinForms verification |
| Linux | `desktop_linux` | AT-SPI2 semantic controls; optional bound-window X11 capture/XTEST input; Wayland semantics only |
| HarmonyOS | `desktop_harmony` | Explicitly not implemented; registration extension point |

Apple Events is an explicit **read-only legacy observer** in this preview; its
positional action references cannot meet the new control guarantees. Start AX
observation to obtain actionable refs. General icon/template/VLM grounding,
HarmonyOS desktop control is not implemented. Windows native capture/input
uses no extra package or paid visual service. Physical SendInput is an explicit
option with target-specific delivery; the verified default fallback uses HWND
messages. Secure desktops and full provider parity are not claimed.

Windows PowerShell/CMD entrypoints are in `scripts/`. PowerShell 5.1 should pipe
JSON into `scripts\qcu.ps1 act -` / `batch -`; the shim preserves UTF-8 and quotes.
Windows process/port probes call Win32 directly without tasklist/netstat startup.
An explicitly bound CDP bridge survives later actions and `batch --observe`.
Windows UIA ref actions stay semantic by default. Native screenshot/coordinates
borrow the bound window; `input_mode:"native"` requests input on a live ref.
`scripts/verify_windows_native.py --output native.json --check-send-input`
reproduces screenshot, pointer, keyboard, Unicode and ownership checks.

## Verification and migration

```json
{"type":"fill","params":{"ref":"<field-ref>","text":"Example"}}
{"type":"toggle","params":{"ref":"<UIA-toggle-ref>","verify":{"kind":"checked","ref":"<UIA-toggle-ref>","equals":true}}}
{"type":"click","params":{"ref":"<button-ref>","verify":{"kind":"text","contains":"Saved","timeout_ms":1500}}}
```

Field fills read back values. UIA selection reads selected state; a raw Toggle
is sent once and needs an explicit desired-state condition. No verification
failure, timeout or lost response causes automatic re-send or transport switch.
See [API](references/api.md), [desktop](references/desktop.md),
[routing](references/routing.md), and [SKILL.md](SKILL.md).

The major preview version reflects stricter contracts: refresh all refs, read
`dispatch_state`/`outcome`, add final-action batch conditions, and migrate Apple
Events actions to AX. `strict_ref` remains accepted; safe ref behavior is now
the default for all web ref actions.

## Tests

See [TEST-REPORT.md](TEST-REPORT.md) for platform results,
reproduction commands and the unconfirmed physical SendInput diagnostic.

```bash
QCU_HOME="$(mktemp -d)" python -m pytest -q
python scripts/verify_macos_ax.py --output macos-ax-result.json
python scripts/verify_windows_uia.py
python scripts/benchmark_ui.py --mode batch --rounds 2 --output result.json
```

The live scripts create disposable native windows. Chromium tests use temporary
profiles/pages. Mock tests do not establish Windows interoperability, provider
behavior, privileges or COM compatibility on real hardware. No universal speed
advantage or complete platform support is claimed.
