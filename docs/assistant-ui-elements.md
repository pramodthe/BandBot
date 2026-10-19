# Assistant UI Elements

The repository contains the complete 151-entry Elements collection published at
<https://www.assistant-ui.com/elements>, captured on 19 October 2026. This comprises
129 component entries and 22 generative composition examples. The upstream MIT
license is retained in `frontend/src/components/assistant-ui/LICENSE`.

For custom calculators, SVG/3D explanations, and interactions beyond the native
vocabulary, see [Interactive answers](interactive-ui.md). That path adds three
read-only skills and `generate_sandboxed_ui`, whose code runs inside a browser
sandbox. Native `present_ui` continues to reject executable code.

## Installed source and discovery

`backend/app/runtime/elements_manifest.json` records every upstream entry, category,
documentation URL, registry item, and local source file. Existing customized elements
and shared helpers were preserved during installation. Generative examples are saved
in `backend/app/runtime/ui_examples.json`, with node keys adapted to the installed
renderer. Their facts are illustrative data, not live service results.

The `ui_elements` agent tool offers category and example discovery without putting
the complete library into every model prompt. `ui_catalog` remains the authoritative
schema for components accepted by `present_ui` and `ask_user`. There are now 55
registered types: the original 35 and 20 additional displays:

CodeDiff, FileTree, LinkPreview, ImageGallery, DocumentReference, ResearchReport,
ComparisonCard, TodoList, FlowGraph, NumberTicker, AudioPlayer, VideoPlayer,
MathBlock, TerminalBlock, EventTimeline, ScoreBreakdown, AgentPlan,
RetrievalChunks, WebSearch, and ActivityGraph.

These displays receive bounded, serializable props. Agent-supplied callbacks, DOM
props, CSS, and actions are rejected. HTTPS checks include nested media/source URLs.
Outputs remain saved snapshots. Media controls, gallery viewing, and disclosure
controls run locally; they do not authorize external work. Terminal output is a
display of evidence, not command execution. Agent plans and todos displayed in a
reply do not modify the durable task queue.

Upstream `$type`/`$key` node spellings are accepted by agent tools and normalized to
`_type`/`_key` before storage. Data records inside props retain their original keys.

## Backend bindings

Admins can open **Workspace interfaces** from their user menu for the selected
channel. The panel provides:

- Activity using existing task, usage, schedule, and result APIs. Stop and retry
  actions use the existing server permission checks and task transitions.
- An optional Assistant UI conversation backed by BAND_BOTS's message store and
  WebSocket drafts. Sending uses the existing message endpoint; stopping uses the
  task endpoint. Agent identity, approvals, generated inputs, attachments, and
  artifacts retain BAND_BOTS's renderers and permission checks.
- Browser speech synthesis and dictation adapters when supported by the browser.
- All 22 example compositions and a searchable index of all 151 entries.

The panel's catalogue endpoint is
`GET /api/channels/{channel_id}/elements`; authentication, channel membership,
and private conversation ownership are checked before returning it. It contains
public library metadata only. Agent output remains available through ordinary
chat, regardless of whether a human opens this admin panel.

## Capability boundaries and remaining integration

Installing every entry is different from implementing every capability its demo
illustrates. Some components are alternative shells for the same conversation;
others require state or operations the current backend does not implement.

The existing backend continues to own approvals, task cancellation/recovery,
schedule cancellation, memory, computers, and connected applications. The Elements
source for those presentations does not replace these services. Runtime capabilities
without an adapter remain unavailable: realtime voice calls, message branching and
destructive conversation editing, feedback persistence, checkpoint rollback, and
individual tool replay. Installing their presentation components does not enable
these operations. The collection includes their source for subsequent integration;
they are not exposed as agent capabilities or falsely presented as working controls.

MCP configuration stays server-side and admin-managed. The upstream client MCP
configuration component is installed but is not a way to grant agents new tools.
Likewise the code-runner component does not execute agent code on the API host.

## Maintenance and checks

Follow `CLAUDE.md` when adding registry components: keep feature-specific network
and state logic outside the element files, preserve the authoritative `surfaces`
and utilities, and keep theme styles in `src/app.css`. The source has both standalone
and runtime-bound (`.aui.tsx`) components. Bind runtime components through the
external-store adapter rather than introducing another model-execution process.

`npm run ui:catalog` exports both the package vocabulary and the schemas in
`components/generated/element-schemas.mjs` to Python. `npm run build` verifies that
contract, TypeScript, the existing script budgets, and production diagram rendering.
The new components are loaded on demand; bundle limits were not increased.

Offline tests cover all 151 source references, all 22 example records, extension
schemas, node normalization, nested media URL validation, agent discovery and
authenticated catalogue access. Browser checks use offline API fixtures to test
rendering and requests without contacting live Band, model, or computer services.
