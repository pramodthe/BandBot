---
name: advanced-visualization
description: Build responsive, accessible sandboxed calculators, charts, simulations, maps and 3D explanations with working controls.
---

Use this guidance only after an explicit human request for a custom/interactive UI.
Enabled skills do not opt a task in. Ordinary answers and native catalogue views do not need this tool.
Each task owns one draft: later `generate_sandboxed_ui` calls replace that draft, not add another card.

# Interactive interface implementation

Adapted from CopilotKit/OpenIntelligentUI's advanced-visualization skill; use BAND_BOTS's sandbox contract below.
Read master-playbook first. Prefer native `present_ui` for supported displays and `ask_user` for required task input.
Use `generate_sandboxed_ui` for custom interactions. The model chooses the format; no additional visualization router is installed.

## Document and execution

Supply `initialHeight`, `placeholderMessages`, `css`, `html`, `jsFunctions`, `jsExpressions` in order, then title/description.
HTML is a body fragment and must be readable before initialization. All styling belongs in `css`.
Do not create document wrappers, meta/base policy tags, iframes, objects or embeds.
Functions and expressions execute as classic scripts. For module libraries:

```js
async function setupScene() {
  const THREE = await import("three");
  const { OrbitControls } = await import("three/addons/controls/OrbitControls.js");
  // Build the scene and controls, observe container size, and dispose on pagehide.
}
function setupFailed() {
  document.getElementById("status").textContent = "The visualization could not load. Try reloading.";
}
```

Initialize with `setupScene().catch(setupFailed);` in `jsExpressions`.
Other mapped modules are `d3`, `chart.js`, and `gsap`. The import map pins library versions.
Scripts/styles may load only from cdn.jsdelivr.net, esm.sh, cdnjs.cloudflare.com and unpkg.com.
Arbitrary fetch/WebSocket requests, nested frames, form submissions, workspace storage, cookies and parent DOM access are blocked.
Load a permitted stylesheet using a `<link rel="stylesheet">` created by script, not a fetch call.
External library failure must leave a readable explanation and visible error state.

The iframe receives `--background`, `--foreground`, `--muted`, `--muted-foreground`, `--primary`, `--border`, and related theme colors.
Compatibility variables include `--color-text-primary`, `--color-text-secondary`, `--color-background-primary`,
`--color-background-secondary`, and `--color-border-tertiary`. Theme changes update without resetting the controls.
Use system-ui/Geist-compatible fonts, restrained colors, and CSS grids that collapse without overflow.
For canvas, resolve CSS variable colors through `getComputedStyle`. Bound canvas height and handle resizing.

## Calculators and data

Every enabled control must do real work. Inputs and calculations stay inside the sandbox.
Use `valueAsNumber`, `Number.isFinite`, domain checks and explicit blank checks. HTML min/max alone is insufficient.
Reject zero divisors, invalid dates and out-of-range values. Clear stale results on validation failure.
Keep internal precision; use Intl.NumberFormat for outputs. Show formulas, units, assumptions and source coverage.
Reset inputs, errors, derived values and visual state together. Never use eval for user-supplied formulas.
For filtering/sorting, derive both from the same source data so one preserves the other.
Use `textContent` for user/retrieved strings. Do not interpolate them into HTML or JavaScript source.
Sample values must be visibly labelled. A chart, table or map is not evidence of live data or a saved backend record.

## Diagrams, animation and 3D

Use SVG/HTML for simpler explanations; read svg-diagrams for labels, connectors and viewBox geometry.
For 3D, use real geometry, lighting, camera controls, antialiasing and a responsive WebGL renderer.
Detect initialization/context failure and show a text alternative. Keep one animation loop; pause when hidden.
Dispose renderer resources on pagehide. Reset state instead of starting a second loop.
Respect prefers-reduced-motion, provide pause/reset, and avoid autoplay that changes the chat scroll position.
Supply text summaries or tables for canvas/SVG. Do not rely on color alone.

## Maps

Prefer the native Map component when pins and routes suffice.
The custom sandbox permits tile images from tile.openstreetmap.org (including a/b/c subdomains),
basemap.nationalmap.gov, and sourced photos from upload.wikimedia.org and thumb.wikimedia.org.
Use the configured provider honestly: USGS topography covers the US; OpenStreetMap can show other regions.
For a custom Leaflet map, load Leaflet 1.9.4 JS/CSS from cdn.jsdelivr.net using script/link elements.
Provide correct visible provider attribution, pan/zoom controls, a bounded responsive height and tile-load error feedback.
Use scrollWheelZoom:false and call invalidateSize after layout. Request visible tiles only, without bulk download.
Use circle markers or inline divIcon markers to avoid unapproved marker-image hosts.
Use supplied/retrieved coordinates, label approximations, and never invent live traffic, geocoding, opening times or road closures.
Connecting itinerary points does not establish driving directions. Label illustrative links and omit unverified mileage/duration.
There is no get_trip_stop_images tool or createTripAnimator helper in BAND_BOTS: use available research tools for evidence,
and implement bounded local animation with one controller and pause/reset rather than inventing service APIs.

## Follow-up bridge and accessibility

Only a clearly labelled user-clicked action should call `BandUI.sendPrompt({text})` or `BandUI.openLink({url})`.
Include validated selected values in text. These are proposals: the human confirms outside the iframe.
Disable the initiating button while awaiting, catch cancellation/network errors, display status, and restore it in finally.
No automatic calls on load, timers or input changes; no secrets, arbitrary tools or approval grants.
For required questions use `ask_user`, which resumes the original checkpoint; widget follow-ups start a new task.
Use semantic headings, real buttons, associated input labels, keyboard focus and role=status/alert feedback.
Implement proper keyboard/ARIA tabs or use simpler aria-pressed buttons. Keep controls usable at 390px width.
The saved interface reopens from Views, but local input state is not synchronized across devices or restored after reload.
