# Repository architecture

BAND_BOTS keeps humans and persistent AI teammates in the same channel context. Band provides messaging and agent identities, Deep Agents execute tasks with the selected model connection, and E2B or Tenki provide cloud computers.

## Source map

```text
backend/
  alembic/            Versioned application-database migrations
  app/
    api/              REST endpoints, WebSockets, and authenticated file access
    band/             Per-user native agent management, Agent API transport and enqueue-only SDK listeners
    computer/         E2B and Tenki lifecycle, observations, actions, desktop and app proxies
    persistence/      SQLModel models, migrations runner, and database access
    providers/        Provider connections, task bindings, verification, and model/service adapters
    runtime/          Deep Agent harness, internal helpers, skills, and tools
    tasks/            Durable queue, worker, approvals, schedules, and usage
    workspace/        Channels, teammates, policies, skills, events, and audit
    auth.py           Sessions, password hashing, and secret encryption
    auth_throttle.py  Signup and login attempt limits
    agent_connections.py Encrypted provider connections and runtime model pinning
    codex_login.py    Isolated Codex app-server sign-in
    openclaw_gateway.py  Approved Gateway transport and native-tool policy verification
    config.py         Environment configuration
    documents.py      Document storage, extraction, and ingestion
    main.py           API process entry
    openai_provider.py Legacy OpenAI settings and task model snapshots
    recovery.py       Recovery email verification and password reset
    worker.py         Worker process entry
  tests/              Offline Python regression tests and provider fakes
frontend/
  src/
    components/
      agents/         Member directory, agent profiles, skills, and identity forms
      chat/           Conversations, composer, channel members, and task controls
      computer/       Agent computers, live desktop, files, and observations
      documents/      Conversation documents, previews, and attachment cards
      generated/      Saved views, reply components, isolated app previews and input
      icons/          Third-party brand marks (Lobe Icons, MIT)
      people/         App contacts, human chats, invitations, and optional Band accounts
      settings/       Providers & APIs, OpenAI quick setup, and connected-app management
      shared/         Reusable application presentation components
      workspace/      App shell, navigation, sign-in, and shared UI types
      ui/             shadcn primitives
      assistant-ui/   Presentational Tool UI elements and their utilities
    hooks/            Shared React hooks
    lib/              Small shared utilities and loaders
    api.ts            Authenticated requests, uploads, and downloads
    store.ts          Workspace state and server-event updates
    types.ts          Shared API contracts
    app.css           Theme tokens and the sole application stylesheet
    App.tsx           Authentication and workspace bootstrap
    main.tsx          React entry
  e2e/                Playwright two-user browser tests
  public/             Static assets served without bundling
  scripts/            UI catalogue generation, bundle checks, and the e2e server
  tests/              Offline JavaScript and compiled-worker tests
deploy/               Container start script and DigitalOcean spec
docs/                 Repository architecture and supporting documentation
.github/              CI workflows, issue and pull request templates
```

The Add agents chooser keeps existing custom teammates on the workspace model connection and assigns connected teammates an `AgentRuntime` row pointing to an encrypted `AgentConnection`. These separate tables avoid altering existing agent columns. Every task snapshots its provider and model atomically. Legacy OpenAI migration skips connected agents. The API continues to enqueue only; the worker selects `ConnectedRuntimeModel` for Codex, Claude, Cursor, and OpenClaw and applies that same connection to task helpers. Provider credentials are refreshed only on the worker, and keys are omitted from serialized connections, agents, bootstrap responses and audit details. Cursor uses the bundled SDK bridge in a clean temporary home with an explicit empty native toolset. OpenClaw accepts only operator-configured Gateway URLs, rechecks the dedicated agent's deny-all native tool policy, and uses a fresh session per model step with the pinned backend model. Neither bridge bypasses app tool approvals or uses provider session history for recovery. See [setup](connected-agents.md).

`runtime/connected.py` adapts vendor reasoning steps to the checkpointed Deep Agents tool loop. `connected_runner.py` executes in a fresh subprocess with a temporary home, an allowlisted environment and native tools disabled. It returns structured tool selections; the vendor process never executes app tools. The graph validates tool names, executes scoped tools and records usage through the existing callbacks. This preserves approval interrupts and replay-safe application tool results without depending on a vendor process surviving an approval pause. Cancellation kills the child process tree. Computers and integrations remain governed by their existing app controllers.

## Runtime boundaries

`codex_login.py` starts an isolated Codex app-server for authentication and model discovery only. Its owner-bound attempt stores a completed session encrypted and cleans up the subprocess on cancellation or shutdown. The native local callback runs on port 1455; remote hostnames cannot start this flow. The worker serializes native Codex credential reads, steps, and rotated-token writes so helpers and channel tasks cannot race refresh. Provider connection screens authenticate before creating a Band identity and are separate from the custom teammate editor.

The API process handles authentication, permissions, messages, WebSockets, Band listeners, and desktop access. Incoming agent requests create durable tasks in the application database. Model reasoning runs only in the separate worker, which stores execution progress in LangGraph checkpoints and commits task results to the application database. The API publishes those results to connected clients.

Visible teammates have Band identities, roles, instructions, and persistent memory. Internal helpers are scoped to one task. Channel membership and policies are enforced on the server; frontend permission controls reflect those rules.

This deployment supports one API process and one worker on a shared local filesystem. The worker may run several independent tasks concurrently while maintaining order for each agent in each channel. Multiple API replicas, independent worker replicas, multi-host execution, and tenant isolation require architectural changes; see the [deployment limits](reference.md#deployment-limits).

## Frontend boundaries

`App.tsx` handles sign-in and bootstrap. `components/workspace/Workspace.tsx` composes the member directory, conversations, modals, and computer panel. Domain components own API calls, polling, and interaction state. Shared workspace UI types live in `components/workspace/types.ts`; API request and response types remain in `src/types.ts`.

Use relative imports within a feature and `@/` imports across features or into `src/api.ts`, `src/store.ts`, and `src/types.ts`. Import components directly rather than through barrel files, so lazy-loaded interfaces, syntax grammars, maps, and diagram workers retain their current bundle boundaries.

`components/shared/` contains application presentation reused across features. `components/ui/` and `components/assistant-ui/` retain their existing layouts and installation workflows. Add theme and component styles only to `src/app.css`.

## Local state and generated files

The default application database, checkpoints, and document originals remain under `backend/` for compatibility. Dependencies, runtime state, secrets, test caches, builds, and QA output are ignored by Git. Reorganizing source must not relocate or delete live databases, checkpoints, encrypted credentials, or user files.

Keep `backend/uv.lock`, `frontend/package-lock.json`, and the generated runtime UI catalogue under version control. The catalogue is a committed contract checked by the frontend build, rather than disposable build output.

App contacts, optional Band account routing, and membership rules are described in [people and Band accounts](people-and-band-accounts.md). `api/people.py` manages app contact requests, human conversations, and channel invitations. `workspace/access.py` authorizes personal conversations and scopes agent visibility and delegation. Legacy shared channels retain their original audience.
