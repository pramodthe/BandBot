# Interactive answers

On explicit request, agents can create custom interactive answers with `generate_sandboxed_ui`:
calculators, simulations, SVG diagrams and Three.js scenes run in a browser iframe
inside the reply. No generated code executes on the API host or worker, and no
cloud computer is needed for these interfaces.

The chat's computer activity card and live badge appear only while the current
task has called a computer tool. Older computer sessions remain accessible from
the header and agent profile; they are not appended below unrelated answers.

## Skills

Three built-in workflows are adapted from CopilotKit/OpenIntelligentUI:

- **master-playbook**: choose text, native components or a custom interactive answer.
- **advanced-visualization**: working controls, calculations, charts, maps, simulations,
  accessibility, responsive layout, library loading and reviewed follow-ups.
- **svg-diagrams**: diagram geometry, labels, connectors, colors and layout checks.

These three skills are **off by default**. Existing rollout assignments have been
disabled; new agents receive no automatic UI skill assignments. Admins can enable
or install them in **Members → agent → Skills**. Only enabled assignments are
mounted read-only for the parent and internal helpers. Skills grant no tools,
permissions, host filesystem access or external-action approval.

Custom rendering is request-scoped. Ordinary questions such as “explain binary
search,” prices and native dashboards do not expose `generate_sandboxed_ui`.
Requests such as “build an interactive flooring calculator” or “use
`generate_sandboxed_ui` to show binary search” opt in for that task. Conversation
history alone does not opt in. The tool also rechecks the stored task request;
UI skills are optional guidance. If a request is not recognized, ask explicitly
for an interactive UI. The existing 151 Assistant UI entries, native tools and
input/approval flows remain available.

The pinned upstream revision, adaptation notes and MIT license are under
`backend/app/runtime/builtin_skills/` and in each skill's supporting files.
This preserves BAND_BOTS's runtime; it does not add CopilotKit, AG-UI transport,
Jev, or another model/router service.

## Tool and rendering

Prefer `present_ui` for supported native displays. Use `ask_user` for required
input that pauses and resumes the original checkpoint. For custom interactions,
call `generate_sandboxed_ui` with these fields in order:

1. `initialHeight`: estimated height, 240–1000 pixels.
2. `placeholderMessages`: 1–4 short loading messages.
3. `css`: iframe-local styling.
4. `html`: readable body fragment without document/policy tags or embedded frames.
5. `jsFunctions`: named JavaScript declarations.
6. `jsExpressions`: small initialization calls.

Provide a title and optional description. The complete payload is bounded to 128 KB.
JavaScript channels are classic scripts: dynamically import modules inside async
functions, without static imports or top-level await. Pinned import-map entries are
`three`, `three/addons/`, `d3`, `chart.js` and `gsap`. Library failures need visible
feedback and readable alternatives. Theme changes do not reset local controls.

Each task has one custom draft, linked atomically to the completed reply.
Subsequent calls replace its source. Applied-call receipts prevent a replay of an
older call from undoing a newer revision. Published replies cannot be mutated;
an explicitly requested follow-up revision produces one new reply. Existing
legacy duplicate versions are hidden from replies and Views listings, while
older saved IDs remain readable. It appears when that reply finishes, rather than streaming
partial HTML. Existing task activity indicates progress while the model writes it.
Saved interfaces reopen from **Views**, and `read_views` gives agents their original
source. Reload resets local inputs; iframe state is not synchronized across devices
or restored after refresh. A new follow-up task creates a new interface, not a DOM patch of a published reply.

## Isolation

The iframe uses `sandbox="allow-scripts"`, without same-origin, form, popup,
download or top-level navigation permissions. Its CSP precedes generated content:
backend fetch/WebSocket requests, nested frames, workers and objects are blocked.
Camera, microphone, geolocation, payment and clipboard permissions are denied.
Generated code cannot read workspace storage, tokens, cookies or the parent DOM.

Scripts/styles can load from the four documented CDN hosts. Tile images are limited
to OpenStreetMap/USGS hosts, and sourced photos to Wikimedia hosts. Media is limited
to data/blob URLs. These are rendering allowances, not authorization to access
connected accounts. Native components and cloud app previews keep separate policies.
Never collect credentials in a widget.

The header offers reload and HTML download. Standalone downloads retain local
controls and the CSP, but cannot send channel messages through the host bridge.

## Reviewed bridge

```js
await BandUI.sendPrompt({ text: "Explain this scenario: length=4 m, width=3 m." });
await BandUI.openLink({ url: "https://example.com/reference" });
```

`Websandbox.connection.remote` aliases the same methods for compatible examples.
Neither method immediately acts on the workspace. The host checks the exact iframe
window, opaque origin, per-document nonce, method, bounds and payload shape. A request
can only propose text or a credential-free HTTPS link.

The host shows follow-up text in an editable field outside the frame. Only a human
click on **Send to agent** calls the authenticated API. A host link button opens a
validated URL with `noopener`/`noreferrer`. Cancellation rejects the iframe promise.
Posting on load cannot bypass host review.

Confirmed messages use
`POST /api/channels/{channel_id}/components/{component_id}/followup`. The server checks
channel membership, private-chat ownership, source task completion, component scope,
the original agent's persistent Band identity and current channel membership.
Message, replay receipt and task commit together. Repeating a request queues no
duplicate task or blind Band resend. Widget text cannot select extra recipients.
Existing ordering, budgets, approvals, cancellation and checkpoint recovery apply.
Follow-ups never supply approval decisions or resume an `ask_user` interrupt.

Generated HTML/CSS/JS is omitted from audit summaries. Confirmed interaction audits
record IDs rather than copying source/input into logs.

## Loading and verification

Restart the API and worker to load the tool and create the additive tables through
normal initialization. No additional rendering service or API key is needed.
Visualization libraries require browser access to their CDNs.

Offline tests cover bounds, replay-safe components, saved Views, atomic follow-ups,
channel/private scope, replacement-agent identity, external approvals, read-only
disabled-by-default skills, explicit request gating, draft revision/replay, legacy duplicate filtering and audit masking. Frontend tests cover CSP construction,
script/style escaping and the bridge schema. Browser QA exercises calculations,
validation, reset, a real Three.js scene, reviewed follow-ups, themes, phone layout
and hostile frame requests without calling live model, messaging or computer APIs.
