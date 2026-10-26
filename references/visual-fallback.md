# Local icon template fallback

Use semantic refs first. This optional local matcher is useful when the target
paints an icon without exposing a control. It recognizes an existing image crop;
it does not infer the meaning of arbitrary icons or run a VLM.

Install `pip install 'qcu[vision]'` (from source: `pip install -e '.[vision]'`).
OpenCV and NumPy load only for `grounding:"template"`; normal observe, semantic
actions, cache, compact reads, batches and native coordinate input keep their
existing paths and dependencies.

1. Observe and bind the intended app/window or browser page.
2. Capture that target with QCU. Save an opaque PNG crop containing distinctive
   icon detail at its original pixel size. Do not crop a solid color, shadow or
   a whole changing status area.
3. Submit an explicit screenshot-fallback action, supplying a result condition:

```json
{"type":"click","params":{"grounding":"template","template_path":"C:/temp/save-icon.png","verify":{"kind":"text","equals":"Saved","timeout_ms":1500}}}
```

Pipe the JSON to `qcu act - --layer screenshot_fallback`. Linux/macOS use their
own absolute template paths. `click`, `double_click`, `right_click` and `hover`
can locate an icon. Native keyboard, scroll and drag retain their existing
explicit coordinate/ref contracts; a template does not locate those actions.
Do not combine a template with a ref or explicit coordinates.

The default normalized correlation threshold is .94, with .03 ambiguity margin.
`template_threshold` accepts .5..1; reducing it increases false matches.
`template_scales` accepts up to nine explicit factors in .25..4, e.g. `[1,1.5,2]`
when capture pixel density differs from the crop. The default `[1]` avoids a
scale search. Matching is pixel based and remains sensitive to theme, font,
animation, hover state and appearance changes.

The matcher requires one candidate above the threshold and rejects competing
matches above the threshold or within `template_margin` of the best score.
It recaptures the same bound target and requires the same matching rectangle,
origin and scale before input. Layout changes, duplicates or disappearing icons
stop before dispatch; there is no silent relocation, transport switch or replay.
This check reduces stale-coordinate errors but cannot make an application atomic
between capture and input. Normal foreground/occlusion/backend gates still apply.

`data.grounding` reports the image-pixel bounding box, center, score, chosen scale
and whether location was rechecked. The backend still reports dispatch/outcome;
a match alone never verifies the business result. A local confidence score is
not a calibrated probability of success.

Only opaque 8-bit PNGs are supported. Templates are limited to 8 MiB/1 megapixel,
captures to 32 MiB/16 megapixels. Transparent/flat templates and invalid sizes or
scales fail before input. General VLM grounding, protected desktops and arbitrary
game input are outside this feature.

Windows live fixture: `scripts/verify_windows_native.py --output result.json
--check-template`. Add `--require-send-input --physical-rounds 10` for distinct
physical key, shortcut, Unicode and pointer result checks. The fixture may focus
only its own disposable window; QCU never activates the application for input.
Linux virtual CI runs the same painted-icon/duplicate scenario with
`QCU_TEST_TEMPLATE=1` under X11 and GNOME 46-50 Wayland.
