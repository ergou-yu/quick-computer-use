# QCU — Quick Computer Use

A faster computer-use skill that prefers the **accessibility tree** over
screenshots. Targets a 5–20× latency reduction per step on typical web
tasks compared to vision-based agents.

> **Status:** MVP skeleton. Web + macOS desktop work; Windows desktop and
> the grounding model are interface-only stubs. WebMCP detection is wired
> up but currently always falls through, because `navigator.modelContext`
> isn't shipped in any production browser yet.

## What's in the box

- **`web_a11y`** — Playwright + CDP. `Accessibility.getFullAXTree` →
  `DOM.getBoxModel` for geometry → `data-llm-ref` injected into the DOM
  for stable, ref-based actions.
- **`webmcp`** — `navigator.modelContext` probe + adapter. Future-proofs
  the system; silent fallback today.
- **`desktop_ax`** — macOS PyObjC. Walks `AXUIElementRef` tree, clicks via
  `Quartz.CoreGraphics`. Honors Accessibility permission state.
- **`desktop_uia`** — Windows stub (Protocol + `uiautomation` hook).
- **`screenshot_fallback`** — last-resort. Borrows the active browser/AX
  handle, calls a registered grounding model (interface only).
- **Router** — priority chain of rules, every decision logged to
  `~/.qcu/telemetry.jsonl` for future ML training.
- **CLI** — `qcu session / observe / act / route / stats / schema`.

## Install

```bash
git clone <repo> qcu
cd qcu
pip install -e ".[macos,dev]"            # or "[windows,dev]" or just ".[dev]"
python3 -m playwright install chromium   # or use the cached binary at
                                        # ~/Library/Caches/ms-playwright/
```

## Try it

```bash
# 1. See the router think:
python3 examples/demo_router.py

# 2. Hit a real page (requires Playwright):
python3 examples/demo_web_skeleton.py https://example.com

# 3. Run unit tests:
pytest tests/

# 4. Run a full session — each `qcu` invocation is a fresh process;
#    state (current URL, cached refs, browser profile) lives in ~/.qcu/.
./scripts/qcu session start --context web
./scripts/qcu act '{"type":"navigate","params":{"url":"https://example.com"}}'
./scripts/qcu observe | python3 -c "import json,sys;d=json.load(sys.stdin);print('elements:',len(d['elements']))"
./scripts/qcu act '{"type":"click","params":{"ref":"ref_0"}}'
./scripts/qcu session end
```

## Why this is faster than vision-only

| Path                              | Cost per action                |
|-----------------------------------|--------------------------------|
| Vision (screenshot → VLM → click) | encode PNG + ~1k-token forward pass + post-hoc OCR |
| **a11y tree → text LLM → ref click** | tens of KB of structured text + ~100-token forward pass + direct locator |

For tasks where the a11y tree is rich (most modern web apps), this is a
**5–20× per-action speedup** and dramatically reduces the cost of
multi-step flows. The router only pays the vision cost when the a11y
tree is blind (canvas/WebGL, missing ref, critical-action confirmation).

## Roadmap

- [ ] Replace the rule chain with a learned classifier trained on
  `telemetry.jsonl` (logistic regression → small MLP → tiny transformer).
- [ ] Real key/keycode mapping for `press_key` / `type` on macOS.
- [ ] Implement `desktop_uia` against `uiautomation`.
- [ ] Pluggable grounding models: OmniParser, Florence-2, Set-of-Mark.
- [ ] MCP server shim so the agent can call `mcp__qcu__observe/act`
      directly instead of via bash.

See `SKILL.md` for the agent-facing contract and `references/` for deep
dives on routing, the API, and desktop permission setup.