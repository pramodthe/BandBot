# Configuration and operations

Detailed setup, product behavior, and deployment notes for BAND_BOTS. Run commands from the repository root unless a package directory is specified. For the product overview and quickstart, see the [README](../README.md).

A Slack-style workspace where humans and AI agents share channels. **Band handles messaging and identity; OpenAI powers agent reasoning and document analysis through Deep Agents; E2B provides visible cloud computers.** Long tasks execute in a separate worker with persistent LangGraph checkpoints.

```text
React UI ←→ FastAPI ←→ Band rooms and agent listeners
                ↓             ↓
             SQLite durable task queue
                         ↓
                Deep Agents worker
                ├─ Researcher / Coder / Critic / Computer helpers
                ├─ LangGraph checkpoints and virtual files
                ├─ Agent/channel memory
                └─ Optional web search and E2B cloud desktop
```

## Requirements and configuration

- Python 3.12+ with uv; Node 20+.
- A Band user API key (`band_u_...`) with access to the Human API.
- An OpenAI API key and access to the model selected for each agent.

OpenAI and E2B are the application's only model and computer providers. No Daytona SDK or direct Anthropic dependency is declared. The locked Deep Agents version requires and imports `langchain-anthropic` and its SDK, so these transitive packages remain installed; the application passes an explicit OpenAI model to the harness and does not use an Anthropic API key. Legacy provider tags and migration checks remain to prevent old checkpoints from replaying external actions under OpenAI.

Copy `.env.example` to `.env` in the repository root and configure:

| Variable | Purpose |
| --- | --- |
| `BAND_USER_API_KEY` | Register visible agents and create/synchronize Band rooms. |
| `OPENAI_API_KEY` | Run the Deep Agents harness and helpers. |
| `APP_SECRET` | Sign sessions and encrypt stored Band agent keys. Set a long random value. |
| `DEFAULT_MODEL` | Default OpenAI model identifier (`gpt-6-luna`, the testing default). Admin settings override environment defaults. |
| `EXA_API_KEY` | Optional web research. Without it, search returns an explicit unavailable result. |
| `COMPOSIO_API_KEY` | Optional Composio project key (`ak_…`). Admins connect Gmail, Slack, GitHub, and other apps in Plugins; agents then use those apps. Without it, Connect stays unavailable. |
| `FAST_CODE_EXECUTION` | `on` (default) gives agents OpenAI Code Interpreter for quick calculations; `off` sends ordinary code to E2B. Document calculations use their dedicated OpenAI analysis tool. |
| `MAX_AGENT_TEAMMATES` | How many visible teammates agents may create on their own; default 10. Teammates people create don't count. |
| `E2B_API_KEY` | Enables isolated cloud desktops, browser/GUI operations, and Python/shell execution. |
| `E2B_DESKTOP_TEMPLATE` | Official `desktop` template by default; custom templates must provide the same XFCE, Xvfb, Chromium, xdotool, scrot, x11vnc, and `/opt/noVNC` stack. |
| `E2B_TIMEOUT_SECONDS` | Runtime fallback auto-pauses the computer; default 1200, range 60–3600. Keep above `AGENT_TASK_TIMEOUT`. Paused computers persist until explicitly deleted. |
| `AGENT_CHECKPOINT_PATH` | Checkpoint store; defaults to the SQLite file `backend/agent_checkpoints.db`. May also be a PostgreSQL URL for diskless hosts. |
| `AGENT_TASK_TIMEOUT` | Execution time limit per attempt in seconds; default 900. |
| `AGENT_RECURSION_LIMIT` | Graph step limit per invocation; default 100. |
| `WORKER_CONCURRENCY` | Tasks the worker runs at once; default 4. Each agent still runs one task at a time per channel. |
| `DEFAULT_TIMEZONE` | IANA timezone for schedules when the humans don't name one; default `UTC`. |
| `MCP_CONFIG_PATH` | MCP integrations file; defaults to `backend/mcp_servers.json`. See **Approvals, schedules, and integrations**. |
| `MODEL_PRICES` | Optional JSON of USD per million tokens per model. Configure current input/output and optional cache_read/cache_write rates; unspecified cache rates use ordinary input pricing. Unknown prices remain unknown. Estimates exclude hosted Python, retrieval and storage charges. Historical records keep their original model IDs. |

The application database defaults to `backend/band_slack.db`. Both processes must use the **same environment, database, checkpoint path, and application secret**. Use absolute paths for overrides. Keep both database files on persistent local storage and back them up consistently.

## Run locally

### OpenAI and documents

Admins open **OpenAI settings** in the navigation rail to save one encrypted key, an allowed model list and a default model. Saving performs small billable image/tool capability checks before enabling the list. `OPENAI_API_KEY` is the fallback when no key is saved. Model choices are snapshotted when tasks are created; a later profile change does not alter a paused task.

Use the chat paperclip or drag files onto the composer, then send the message. Files stay private to the uploader until sent. Mention an agent in channels, or send directly in a private chat, to request analysis. **Documents** shows processing, preview, download, retry and removal. Supported formats are PDF, DOCX (text/tables), XLSX, CSV, UTF-8 TXT/Markdown, PNG, JPEG and WebP, up to 25 MiB each and ten attachments per message. PDF processing is bounded to 300 pages, workbooks to 200,000 cells and extracted text to three million characters. Split larger files. Word charts/images requiring visual interpretation must be supplied as PDF.

The worker extracts source locations, transcribes scanned pages/images with OpenAI vision, uploads originals and indexes searchable text in one OpenAI vector store per channel or private chat. Whole-document analysis uses complete originals or ordered text reads; targeted retrieval alone is not a whole-document summary. Spreadsheet calculations give OpenAI Python the full original workbook, including rows beyond direct file input previews. Generated files are copied into application storage for authenticated downloads. E2B remains the visible browser/desktop and is not required for document analysis.

Questions about uploaded or saved conversation files go directly to the built-in document tools, including follow-ups without new attachments. Connected-app discovery is for external sources such as Google Drive or Gmail. Missing or processing conversation files do not automatically trigger a search of external accounts.

Originals and metadata must be backed up together. `DOCUMENTS_PATH` defaults to `backend/documents`; keep it on the shared persistent filesystem used by API and worker, or set it to `database` so originals live in the application database. Abandoned staged uploads expire after a day. Removal hides documents immediately and schedules provider cleanup; stale retrieval results are filtered against local access and deletion state. Uploaders and admins can remove files. Ingestion jobs recover after worker restarts, and processing tokens count against channel budgets. Token cost estimates exclude OpenAI storage and hosted tool charges.

Before the first OpenAI deployment, let running Claude tasks finish and back up both databases. Agent identities, conversations, memories and computers remain. Old paused/failed Claude checkpoints are retained for inspection; cancel a waiting task and choose Retry/Resume to explicitly start a new OpenAI task. They are never automatically replayed through the new provider. New OpenAI tasks retain normal checkpoint resume behavior.

Install dependencies:

```bash
cd backend
uv sync --locked
```

Run these in separate terminals:

```bash
# Terminal 1: API and Band listeners
cd backend
uv run uvicorn app.main:app --reload --port 8000

# Terminal 2: durable task worker
cd backend
uv run python -m app.worker

# Terminal 3: frontend
cd frontend
npm ci
npm run dev
```

Open http://localhost:5173. Alternatively, run `npm run build` from `frontend/` before starting the API; FastAPI serves that build at http://localhost:8000.

Sign up and mention `@Master` in `#general`. The app creates the default room and Master on Band. Local mentions save jobs atomically with the message; external Band turns also become durable jobs. Jobs stay queued if the worker is stopped, and accepted local tasks can run even when Band is offline. Master can delegate to internal researcher, coder, critic, and computer helpers and returns a combined answer. Create additional **visible teammates** from the sidebar when you want a persistent agent with its own role and memory. Internal helpers are task-scoped and do not register separate Band identities.

Passwords accept 6–128 Unicode characters. New hashes use the [bcrypt library's documented SHA-256/base64 preprocessing](https://github.com/pyca/bcrypt#maximum-password-length) so the entire password is checked, including characters beyond bcrypt's 72-byte input limit. Existing bcrypt accounts remain compatible without a password reset.

In **Members → Create agent**, choose an avatar, name and optional specialty. The specialty appears on the member profile and guides the agent’s Deep Agents prompt. **More settings** holds the model, instructions and connected-app selection. New agents created here use a custom role, join no channel and have no connected-app access unless you explicitly select those options. Add a teammate later from a channel’s Members panel; edit its specialty, avatar and other settings from its profile. Existing role-based teammates keep their roles and access.

An agent’s profile is its settings page, including when opened from a chat. Edit Markdown instructions inline in **Overview**, manage reusable workflows in **Skills**, select connected-app permissions in **Apps**, and view or take over its channel computers in **Computer**. The profile menu holds identity settings, restart and admin-only deletion. Skills remain admin-only; model changes require an admin. Plugin access can be restricted to named apps or explicitly include every current and future connected app. Saving one section leaves the other sections unchanged.

The workspace opens in **Conversations**. Click an agent’s name or avatar in chat to open its profile beside the conversation; smaller screens use a dismissible sheet. **Overview**, **Skills**, **Apps**, and **Computer** group its settings, and **Message agent** opens a private conversation. Agent-to-agent chatter starts collapsed; its checkbox and the hidden-message link reveal it. The compact **Agent tasks** panel expands for progress, results, retry and stop controls. The sun/moon button in the navigation rail switches between charcoal and light themes and remembers the choice in this browser. On phones, the back button returns from a full-width conversation or member profile to the directory and navigation.

**Members** opens a team overview with real teammates, profile and message actions, people, and shared channels. Agents have six distinct vector characters with a visible character picker in their profile editor. Characters blink and respond to hover or keyboard focus; working motion and attention labels follow known live task activity. Reduced-motion preferences disable character animation.

Files exported by an agent are attached to its completed chat reply. Exported `.html` and `.htm` apps render immediately in **Webpage preview**, including when the agent's computer is paused. The assistant-ui **WebPreview** provides reload, an expanded preview, desktop/phone sizing, source code, and download controls. When a reply also has live apps, its **Preview** tab shows the saved webpage and its **Live app** tab opens the server version in the same place. Multiple registered servers share one selector. Finished computer screenshots are collapsed under **Computer activity**, with **Open computer** still available. Existing saved exports also appear without rerunning the task. Exported HTML runs in an opaque-origin sandbox, separate from workspace storage and APIs; API connections, form submissions, nested frames, popups and parent navigation are blocked. HTTPS images, fonts, styles and scripts can load.

**Live server apps** appear in chat when the agent calls `publish_app`. Use **Open live preview** in the **Live app** tab to use the app, including its assets, API requests, cookies, and WebSockets. Expansion and desktop/phone resizing preserve the running app’s client state. **Disconnect** releases your session; leaving the conversation also disconnects. The computer stays awake while a preview is open, with a 15-minute lease renewed every five minutes. Stop computer overrides previews; reconnect explicitly to resume. Runtime and storage can incur E2B charges. For an existing app, expand **Computer activity** and choose **Preview app** and enter its server port and start page. The server must already run on `0.0.0.0` in the agent’s computer, usually as a background process. Browser requests must use relative paths on the registered port; proxy a separate backend through the app server. New computers accept the private gateway’s localhost host header; an older Vite server may need its private E2B hostname in `server.allowedHosts`.

The gateway supports one registered app port per preview (1024–49151, excluding desktop/debugging/infrastructure ports), with up to six apps per task. Each connection uses an unpredictable, expiring hostname bound to its viewer, channel, agent identity and sandbox. It checks channel membership, DM ownership, computer-control policy and takeover ownership. HTTP streams and WebSockets recheck access at least every five seconds. E2B ingress remains private and its credentials stay server-side. Preview hostnames are bearer credentials: keep them private. Each app runs on a separate origin and cannot read workspace storage; nested frames, popups and parent navigation remain blocked. HTTP uploads are limited to 32 MB and WebSocket messages to 8 MB. API traffic must stay on the app origin; HTTPS images, fonts, styles and scripts can load.

Locally, `APP_PREVIEW_BASE_URL` defaults to `http://localhost:8000` and Chromium resolves `*.localhost` to loopback. Production requires a dedicated domain on a separate site (for example `https://preview.example.net`), wildcard DNS and TLS for `*.preview.example.net`, and an HTTP/WebSocket reverse proxy that preserves Host and routes to the same API process. Do not route that domain to a second API/worker instance. Configure the real workspace Origin through the normal proxy; only that origin can embed its preview. The computer activity thumbnail continues to show the agent’s last observation.

Agents can also hand work to visible teammates with `ask_teammate`. The request appears in the channel as agent chatter ("to Writer"), the teammate runs after the requester's turn ends, and by default its answer goes back to the requester as a new task so it can combine results or hand off the next step. With `report_back` off, the teammate answers the humans directly. Each original request allows up to 6 hand-offs. Hand-off requests and report-back answers stay local and are not copied to Band. Cancelling a task does not cancel hand-offs it already made; cancel those from the task panel. The bar above the composer shows each queued or running agent and its current step, updated live over the WebSocket.

Master can also grow the team. When no teammate's role fits, it calls `create_teammate` with a name, role, and instructions. The worker records a `TeammateRequest`; the API process registers the agent on Band with Master's model, adds it to the channel, and posts "Master created …". The new agent appears in the sidebar and member list right away, and Master hands off to it with `ask_teammate`. A replayed tool call returns the same teammate, an existing name is reused instead of duplicated, and requests fail with a clear message when Band is offline or `MAX_AGENT_TEAMMATES` is reached. Creation is not behind an approval, and teammates can't be created inside a direct message.

**Outside Band agents** (a Claude Code or Codex session, or another app's agent on Band) can join a channel from **Outside Band agents → Invite a Band agent** in the member list, which lists the Band agents your account can add. Agents in the channel reach them with `ask_teammate` by name or handle: the request is sent once through Band from the asking agent's identity, mentioning only the outside agent, and their reply appears in the channel through the Band poller. Report-back isn't guaranteed: the asking agent gets the answer as a new task only if the outside agent mentions it in the reply (the request asks it to). If Band doesn't confirm the send, the agent is told not to resend.

The **Agent tasks** panel shows progress, cancellation, resume controls, results, downloadable virtual files, and saved memories. Use **Forget** to remove an incorrect memory; ask the agent to save a corrected fact. Task state refreshes through authenticated polling. The API also forwards worker replies over WebSockets so other channels receive unread updates.

## Agent skills

Open **Members → an agent → Skills** to manage reusable workflows. Workspace admins can add library skills, create and edit custom instructions, enable or disable installed skills, remove them from an agent, and export ZIP bundles. Removing a skill from one agent keeps it in the library; deleting a custom skill from the library removes it from every agent. Built-in examples are read-only and can be copied to customize. Editing a shared skill updates every agent using it.

Import a `SKILL.md`, a ZIP, or a skill folder. A `SKILL.md` has YAML frontmatter with a lowercase hyphenated `name` and a `description`, followed by Markdown instructions. Imports support one skill with up to 40 UTF-8 text files, 1 MiB unpacked, and a 2 MiB upload. Supporting text files are preserved and readable by agents; imported scripts are not automatically executed. This library is local to the workspace, with no external marketplace connection.

Enabled skills are loaded at the start of a task through Deep Agents' native skill loader, including the agent's internal helpers. The agent sees the skill names and descriptions, then reads relevant instructions and supporting files when needed. Skills are mounted read-only under `/skills/`; they do not grant tools or change approval and permission policies. Changes apply to new tasks. Agent assignments and skill definitions persist in the application database.

## Tools, memory, and recovery

- Planning, virtual file management, and specialist delegation use Deep Agents. Virtual files live in checkpoints; they do not expose the server filesystem. Exported task files are limited to 20 files and 200,000 characters total.
- `channel_history` reads recent messages from the current channel. `recall_memory` and `remember` persist knowledge scoped to the visible agent and channel, with source task IDs and timestamps. This is explicit keyed memory, not semantic search. Specialist helpers can read memory; the parent agent writes it.
- Web research uses [Exa Search](https://exa.ai/docs/reference/search) with a 30-second timeout, up to five sources, and up to 6,000 text characters per source. Set `EXA_API_KEY`; retrieved text is treated as untrusted source material.
- `fetch_page` reads one public URL as text through Exa's contents API (up to 20,000 characters) without starting a computer. Exa fetches from its own servers, so private and localhost addresses are not reachable. Agents are instructed to use the cheapest sufficient tool: knowledge, memory, and channel history; then search and page fetch; then code execution; and the E2B desktop only for logins, forms, interactive pages, or visual checks.
- Local `@mention` tasks include the channel's last 30 messages (up to 24,000 characters) as untrusted context, like Band turns.
- Quick calculations use OpenAI Code Interpreter in the Responses API, available to the parent and coder/critic helpers. Its Python container is separate from E2B and is scoped to the task history. Document calculations use `analyze_documents(calculate=true)` to supply complete originals. Chart requests return verified labelled data for the native assistant-ui `Chart` through `present_ui`, with axes, source coverage and aggregation explained. Large datasets are aggregated across all relevant rows rather than silently sampled. Static chart files are generated when requested as exports, alongside the native chart unless the person asks for a file only. Generated files are copied into the conversation's Documents library (up to 25 MiB per file); their links survive the analysis container's expiration.
- Work that needs the agent's own files, installed packages, the network, or a desktop uses E2B; no local desktop container is required. See **E2B computer use** below.
- Replies stream into the channel while the agent writes them. Only the parent agent's final answer streams (text it writes before a tool call is dropped); helpers, report-backs to teammates, and interim statuses don't stream. The draft lives in the `MessageDraft` table and is removed when the final message is saved or the task fails, is cancelled, or pauses for approval. Band still receives only the final message.
- Each task has its own checkpoint thread. Worker restart requeues interrupted tasks and resumes saved progress. Provider requests have bounded retries; task failures require **Resume**. The parent has a 40-model-call limit per invocation and 120 per task thread; computer helpers have a 30-call limit and other named helpers have a 12-call limit per invocation, in addition to graph/time limits. These are execution limits, not dollar budgets.
- Local replies are committed with task completion. Band replies get one automatic send attempt: uncertain outcomes are marked `unknown` and are never blindly resent. A local answer remains available even when Band delivery is uncertain. Full local answers may exceed the 8,000-character Band copy.

## Reconnect and message retries

The workspace keeps one API process. Its WebSocket fan-out is in memory, so events sent while a browser is disconnected are not replayed. When the socket opens again, the app reloads `GET /api/bootstrap` and applies that snapshot, then replays only socket events whose `cursor` is newer than the snapshot. A slow snapshot cannot put a private channel back after the member has left, and it cannot overwrite a newer live update. Pending channel invitations and incoming contact requests are part of the snapshot, so they show up in the conversation list without a page reload.

`GET /api/channels/{id}/messages?after={message_id}` returns messages newer than one the client has already seen. Those are counted once. Opening the conversation clears that count. A message the client has already applied does not increase it again.

Human sends may include `client_id`: 8–80 characters from `A-Z`, `a-z`, `0-9`, `_`, and `-`. The server stores it in the `client_message` table, unique per sender and conversation, in the same transaction as the message and its agent tasks. The same id and the same text and attachments return the original message and do not enqueue another task. A different payload with that id returns 409. The id is scoped to the authenticated sender and that conversation: it cannot fetch another person's message, and a retry after leaving a private channel is rejected with the same 404 as any other request. Omitting `client_id` preserves the previous behavior, one new message per request, so older clients keep working. New clients show Sending, then the saved message, or Not sent with Retry. Retry uses the original `client_id`.

`sync_cursor` and `client_message` are new tables. Startup creates them; existing message rows are not rewritten. Update the API and the web app together so retries are idempotent. An uncertain Band delivery stays uncertain: a message retry never resends a Band post whose outcome is unknown.

This cursor orders one API process's events. It does not add multi-process fan-out.

## Assistant UI Elements

On explicit request, agents can build custom calculators, simulations, SVG diagrams and 3D explanations with `generate_sandboxed_ui`. The three adapted UI skills are off by default and can be enabled per agent. Each task produces one custom card; revisions replace its draft, and saved interactive answers reopen from **Views**. The 151 native Assistant UI entries remain available. Widget follow-ups require a human review outside the isolated iframe; ordinary approvals still apply. See [Interactive answers](interactive-ui.md) for skill controls, sandbox policy and restart instructions.

The complete 151-entry Assistant UI Elements collection is installed, with 55 registered UI types and 22 discoverable composition examples. Admins can open **Workspace interfaces** from their user menu to browse the collection, channel activity, and the optional conversation interface. See [Elements integration](assistant-ui-elements.md) for backend bindings and capabilities that still require additional implementation.

## E2B computer use

The conversation shows the agent's latest real screenshot using assistant-ui's ComputerUse element, with a persisted action trail and pointer positions. **Open computer** opens Screen, Live desktop, Files, and Activity. These observations also survive a page reload; typed text and URL credentials are not included in the action labels.

E2B provides a full Linux desktop in a separate microVM for each agent and channel, with a browser, terminal, and persistent files. Outbound internet access is enabled. Inbound desktop access requires E2B's private traffic token, which stays encrypted on the server; the app proxies noVNC over authenticated, short-lived links. See [E2B desktop](https://github.com/e2b-dev/desktop).

Open **Members → an agent → Computer** to choose one of your channels or direct chats and see that agent's desktop. The panel includes the last screen, a live desktop, files, and activity for the latest task. **Open chat** returns to the selected conversation. Existing computers can also be opened from the channel header and a task's **Open computer** menu. Browsing the profile never starts or creates a computer; **Connect** resumes an existing paused desktop. An agent first creates a computer when a task needs it.

1. Add `E2B_API_KEY` from [E2B dashboard](https://e2b.dev/dashboard) to `.env`, then restart API and worker. No application credentials are copied into the sandbox. No cloud resources are created until a computer/code tool is actually called.
2. Use the default `E2B_DESKTOP_TEMPLATE=desktop`. The model must accept images. The adapter uses E2B's async SDK and starts the template's XFCE display and noVNC services idempotently.
3. Try: `@Master use the computer to open https://example.com, inspect the page, and report what you see.` Master delegates desktop work to its computer helper. The **Computer** button in the task panel opens the agent's computer for that channel:
   - **Live** embeds the desktop (E2B's private noVNC on port 6080) through a signed link valid for 15 minutes; use **Reconnect** after it expires. Opening it resumes a paused computer but never creates one. **Take over** gives you the mouse and keyboard and pauses the agent's clicks, typing, commands, and file writes for up to 15 minutes (it can still take screenshots); **Hand back** ends it. Both are announced in the channel.
   - **Latest screenshot** shows the agent's last observation.
   - **Files** browses the computer, downloads files, and uploads files (up to 25 MB) to its `uploads` folder; uploads are announced in the channel so the agent knows where they are.
   - **Stop computer** pauses it when no task is running there, preserving its files and applications.

Available tools:

| Tool | Behavior |
| --- | --- |
| `computer_screenshot` | Returns the actual image to the model, full-screen dimensions, and a fresh screen ID. |
| `computer_open_browser` | Opens an HTTP(S) URL in the remote graphical browser and returns its screenshot. |
| `computer_action` | Click, move, drag, scroll, type, press a key/modifiers, or wait; returns a new screenshot. |
| `run_python`, `sandbox_exec` | Execute Python or shell commands on the remote computer, with a 30-second limit and bounded captured output. |
| `sandbox_write_file` | Transfer text into an absolute path in the computer. Virtual draft files are separate from this filesystem. |
| `sandbox_export_file` | Copy a UTF-8 computer file into downloadable task artifacts, within the existing export limits. |

Each agent has one persistent computer per channel, matching the scope of its memory: files, installed tools, and browser logins carry over between tasks in that channel and never cross channels. Its identity is stored in the `TaskSandbox` table and its labels name the workspace, the agent's Band identity, and the channel; reconnecting verifies them. If the computer was deleted, the next tool call starts a fresh one and tells the agent that earlier files are gone. Deleting an agent forgets its computer records; cloud runtime auto-pauses but its disk remains until explicitly deleted in the E2B dashboard.

Desktop operations are serialized. A screenshot ID is consumed **before** each mouse/keyboard action, so a replayed tool call cannot blindly repeat the same input. Another helper or shell command invalidates the prior observation; the agent must take a new screenshot. These guards cover the computer API; arbitrary shell commands still require care with external side effects. Delegate a single GUI workflow at a time.

The worker normally pauses the computer on success, failure, cancellation, and graceful shutdown. Files, installed packages, running applications, and browser sessions survive pause/resume. Opening a live viewer creates a 15-minute viewing lease, so task completion keeps the desktop available while you watch. **Stop computer** overrides that lease. Reconnecting renews it. If the host crashes, E2B's configured runtime timeout auto-pauses the desktop. Paused disks remain until explicitly deleted and may incur storage charges; manage them in the [E2B dashboard](https://e2b.dev/dashboard). See [E2B persistence](https://docs.e2b.dev/sandbox/persistence).

Existing Daytona disks are not copied or deleted by this migration. E2B records use a separate identity namespace; old `DAYTONA_*` settings are ignored. Export any files you need from the old provider and remove unused resources there separately.

Screenshots and exported files can contain task data. Desktop inspection, live view, and file access require channel membership and return `Cache-Control: no-store`; screenshots also persist in task checkpoints. The live link carries a short-lived application signature bound to the viewer, task, and computer; connections recheck channel membership, DM ownership, computer policy, and take-over ownership. Watchers reach a separate VNC server that rejects input. Only the active **Take over** owner reaches the interactive desktop; giving control back, expiry, or a change in owner closes that connection. The E2B key and private ingress token never reach the browser. The application signature is a bearer credential, so keep live links private. Under a channel's "Admins only" computer policy, only admins get a live link (to watch or take over), hand a computer back, or stop it; members keep the Screen and Files tabs. Agents are instructed to request approval before submitting forms, sending messages, payments, or other external changes (see below), but the computer cannot technically block a click; use accounts and tasks appropriate to that access model.

## Approvals, schedules, and integrations

**Approvals.** Agents call `request_approval` before actions with external effects. The task pauses with status `waiting`, posts an "Approval needed" message, and frees its worker slot. Channel members approve or deny on that message (or only admins, when the channel's policy says so), which shows the decision once it is made; the task resumes from its checkpoint with the decision, including inside a helper such as the computer helper. Waiting tasks can be cancelled.

**Schedules.** Ask an agent for something later or on a cadence (for example "every weekday at 9am, summarize AI news"). It calls `schedule_task` with `once`, `daily`, `weekdays`, `weekly`, or `interval` (at least 15 minutes) in an IANA timezone (`DEFAULT_TIMEZONE` when unspecified). The worker queues each run as a normal task in that channel. Runs missed while the worker was stopped fire once on restart rather than backfilling. A channel can hold 20 active schedules; cancel them from the task panel or by asking the agent.

**Tables, charts, and spec sheets.** Agents attach structured results to their reply with `show_table` (sortable columns with number, currency, percent, delta, date, boolean, badge, or link formats; up to 12 columns and 100 rows), `show_chart` (one metric's value, change, and series as an area, line, or bar chart), and `show_spec` (one item's attributes as labeled rows). The tools validate their input, and the app draws them under the reply text with [Tool UI](https://www.tool-ui.com) elements. Band and other clients receive only the reply text, so agents state the conclusion and its key figures there. Components from a failed or cancelled task are never shown.

**Generative UI and human input.** Agents use the real `@assistant-ui/generative-ui` catalogue through `ui_catalog`, `present_ui`, and `ask_user`. The catalogue includes layouts, tabs, facts, charts, sortable tables, timelines, diagrams, maps, and input controls. The app supplies form submission and task controls. Generated UI uses the workspace theme; native React components render validated JSON, without generated HTML or scripts. `present_ui` saves a composed reply; reopen it from **Views** in the conversation header. Existing `show_view` dashboards and older decision cards remain readable.

Native charts keep every plotted point while showing a readable subset of x-axis tick labels. **Show chart data** opens the exact values and labels used by the plot; it does not rerun the agent or change the saved data. Source links in generated reply descriptions open the permission-checked document preview.

When information or a decision is missing, `ask_user` calls LangGraph `interrupt()` inside the Deep Agents tool. The original task stays **waiting**, its checkpoint survives restarts, and the worker slot is released. A channel member fills in the controls, adds optional steering, and chooses **Continue**. Validation, the shared receipt, human response message, and requeue of that same task commit together. The worker resumes with `Command(resume={interrupt_id: answer})`; it does not start a replacement task. Multiple questions and external-action approvals must all be answered before the run continues. Identical retries reuse the receipt; conflicting responses return 409. Cancellation closes the card, and an explicit task resume reopens the unanswered checkpoint.

Membership and private-message ownership are enforced server-side. A clarification answer never approves an external action or changes tool permissions. UI trees allow up to 200 nodes, 12 nesting levels and 64 KB; input surfaces allow 24 named controls, 4,000 characters per text value, 16 KB total answers and 2,000 characters of steering. Password controls are refused. Saved replies are snapshots; shared editable databases and arbitrary app code are not provided by this integration.

The Python prop schemas are generated from the installed assistant-ui package: run `npm run ui:catalog` in `frontend/` after upgrading it. `npm run build` checks for catalogue drift before compiling. The React renderer is standalone; the existing Band transport, Python Deep Agents worker, SQLite checkpoints and approval flow remain responsible for agent execution.

**Plugins.** Set `COMPOSIO_API_KEY` to a Composio project key. An admin opens **Plugins** in the sidebar, searches for an app, and chooses Connect. The browser opens Composio's sign-in page; the list refreshes when you come back. Disconnect revokes that app. Agents receive Composio's search, schema, and execute tools rather than one tool per action. A new agent may use every connected app. Uncheck apps on the agent form to limit it, for example Gmail only for a mailing agent. Execute calls still pause for approval. The project key stays on the server.

**MCP integrations.** Copy `backend/mcp_servers.example.json` to `backend/mcp_servers.json` (git-ignored) and list servers with `stdio`, `streamable_http`, or `sse` transport. Put secrets in `.env` and reference them as `${VAR}`; a server whose variables are unset is skipped. Per server, `approval` is `writes` (default: tools not marked `readOnlyHint` pause for approval), `always`, or `never`, and `agents` optionally limits which agents get its tools. Tools appear to agents as `<server>__<tool>`. The worker loads tools at the start of each task attempt (a 30-second timeout per server; unreachable servers are skipped and logged). If the worker crashes during an approved write, the write may repeat when the task resumes. A `composio` entry in that file is ignored while `COMPOSIO_API_KEY` is set, so the workspace session is the one agents use.

## Roles, activity, and spending limits

**Roles.** The oldest account is the workspace **owner**; everyone else is a **member** until the owner makes them an **admin** (Make admin, in the channel's People list). The owner's role can't change. With nothing configured, everything works as before: any channel member can approve, take over, cancel, and resume.

**Channel policies.** Admins set two policies per channel under **Permissions and spending** in the member list: who approves agent actions (cancelling a task that waits for approval counts as deciding it) and who watches or takes over agent computers live (any member, or admins only). Deleting an agent, changing an agent's model, changing policies, and setting budgets always require an admin. Controls a person can't use are disabled with the reason shown.

**Activity.** Every tool call an agent or its helpers make (including OpenAI Code Interpreter), approval requests and decisions, hand-offs, schedules, computer take-overs (including ones that run out of time), hand-backs, uploads, downloads, and stops, agent and policy changes, and task cancels and resumes are recorded in order. Open **Channel activity** in the task panel, or **View activity** on one task. Arguments are short summaries: long values are cut; values under keys named like keys, tokens, passwords, cookies, or authorization are hidden at any depth; and secrets inside text are masked (`Bearer …` and `sk-…` style tokens, credentials in URLs, `Authorization`/`Cookie` header values of any scheme, `-p<password>` and `--password`/`--token` flags, and `password=`/`token: "…"` values). Masking leans toward hiding too much, but it is pattern-based, so agents should still keep secrets out of tool arguments. Provider errors are never recorded. Channel members read `GET /api/channels/{id}/audit` (`after=<id>` returns newer events oldest first); people outside the channel can't. Admins read `GET /api/audit` (all channels except other people's direct messages).

**Spending limits.** The worker stores the token usage of every model call in a task once per response, through a model callback: the agent's calls, its helpers', and the internal summarization calls made when a conversation gets long. The task panel shows tokens per task, and the member list shows today's tokens for the channel and each agent (UTC day). Admins set daily token budgets per channel and per agent (an agent's budget counts all channels). When a budget is used up, queued tasks wait with "Paused: … daily budget" in their progress, and a running task stops like a cancellation with a channel notice. Raising or removing the budget lets queued tasks run; resume a stopped task to continue from its checkpoint. Budgets are checked before every model call, but usage is only known when a call returns, so calls already in flight when a budget runs out still finish: at most one per running agent or helper, and with several workers or parallel helpers that adds up. A call cut off by a cancel, stop, or time limit is billed by the provider but never reports its usage, so it isn't counted. Set `MODEL_PRICES` to see estimated cost.

## Validation

```bash
cd backend
uv run alembic upgrade head
uv run pytest

cd ../frontend
npm test
npm run build
npm run e2e
```

Offline tests exercise actual Deep Agents tool calls and helper delegation using a scripted model, checkpoint recovery across reopened connections, memory isolation, job deduplication, cancellation, authenticated task controls, role and policy permissions, the activity log, and budget enforcement. The computer tests use a fake E2B SDK to check lifecycle/ownership, screenshot delivery to the real harness, replay-safe input, isolation, and authenticated desktop inspection. Live Band/OpenAI/Exa/E2B calls require configured integration checks.

`npm run e2e` starts a temporary API, worker, and Vite and runs the two-person Playwright suite. It does not call Band or a model provider. See [Operations](operations.md).

Frontend tests use Node's built-in runner for diagram-worker concurrency, errors, timeouts, and syntax-asset integrity. Diagram layout runs in one shared worker loaded only when a diagram is shown, keeping the layout engine off the UI thread. Large C++ and Emacs Lisp grammars load as complete JSON assets, and Oniguruma loads its standalone WASM binary. All languages and diagram types remain available. The production build enforces a 500 kB limit per page script and a separate 2 MB limit for the on-demand diagram worker, with Vite's warning threshold unchanged. It also tests the compiled worker across six diagram types under strict worker-global semantics.

## Project layout

See [the architecture guide](architecture.md) for the source map and runtime boundaries, and [CONTRIBUTING.md](../CONTRIBUTING.md) for development and verification conventions. GitHub Actions runs the offline backend suite and frontend tests/build on pushes to `main` and pull requests.

- `backend/app/main.py`: ASGI entry (`uvicorn app.main:app`). Routes live in `backend/app/api/http.py`.
- `backend/app/workspace/`: channels, messages, and the agent profiles people create (`service.py`, `roles.py`, `events.py`), workspace roles and channel policies (`permissions.py`), and the activity log (`audit.py`).
- `backend/app/band/`: Band registration (`org.py`) and the listener that only enqueues (`listener.py`).
- `backend/app/tasks/`: durable queue, hand-offs, approvals (`queue.py`), schedules (`schedules.py`), token usage and budgets (`usage.py`), and the worker (`worker.py`, started with `python -m app.worker`).
- `backend/app/runtime/harness.py`: builds one Deep Agent per task. Helpers are `runtime/helpers/`. Tools are `runtime/tools/` (memory, channel, web, teammates, schedules, approvals, computer, and MCP).
- `backend/app/computer/`: E2B desktop lifecycle, observations, actions, and files.
- `backend/app/persistence/models.py`, `backend/app/auth.py`: database models, session authentication, and key encryption.
- `backend/tests/`: offline regression tests.
- `frontend/src/components/`: application components grouped into `agents/`, `chat/`, `computer/`, `documents/`, `generated/`, `settings/`, `shared/`, and `workspace/`. Tailwind 4 styling uses [shadcn/ui](https://ui.shadcn.com) primitives in `ui/` and [Tool UI](https://www.tool-ui.com) elements in `assistant-ui/elements/`. The UI uses a slim navigation rail, a member/conversation directory, expressive vector agent characters, and spacious profile panels, with Geist and charcoal surfaces by default. A remembered light theme is also available. Theme tokens and global styles live in `src/app.css`, the only stylesheet. New primitives come from `npx shadcn@latest add <name>` and new elements from `npx shadcn@latest add @assistant-ui/elements-<name>`; see CLAUDE.md for the post-install steps.
- `backend/alembic/`: versioned application-database migrations. Checkpoints stay in their own SQLite file. See [Operations](operations.md).

## Deployment limits

The product is now called **BAND_BOTS**. Existing database filenames, browser storage keys, session signatures, and cloud-computer ownership labels retain their earlier identifiers so upgrades keep accounts, preferences, and saved computers connected. Renaming the display brand does not require a data migration.

`DATABASE_URL` can name SQLite or PostgreSQL. `AGENT_CHECKPOINT_PATH` may be a SQLite file or a PostgreSQL URL. Changing the application database does not enable a second API or a second worker. See [Operations](operations.md) for the migration, backup, and rollback steps.

This version supports **one API process and one worker on a shared local filesystem**. The worker runs up to `WORKER_CONCURRENCY` tasks at once, at most one per agent per channel, so messages to the same agent in a channel are answered in order. Parallel tasks multiply concurrent OpenAI requests and can start several E2B computers at once; lower the setting if you hit rate limits or cost limits. An exclusive lock prevents a second worker using the same checkpoint file. Run the worker under a process supervisor for automatic restart. Do not run independent worker replicas or different checkpoint paths against the same queue. Multi-host workers need a transactional leased queue and a shared checkpoint backend before deployment.

All channels are shared within this single workspace; this is not tenant isolation. Task controls and memory deletion require channel membership; approvals, live computer access, and take-overs follow the channel policy, and the activity log is for channel members. Roles are workspace-wide (owner, admin, member), not per channel, and budgets count tokens, not dollars. Semantic memory retrieval and multi-host execution are not implemented. Secrets and local database/checkpoint files must stay out of Git. Keep `APP_SECRET` stable; changing it invalidates encrypted agent credentials. Deleting a visible agent also requests deletion on Band, including its Band history.
