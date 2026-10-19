# Repository architecture

BAND_BOTS keeps humans and persistent AI teammates in the same channel context. Band provides messaging and agent identities, OpenAI and Deep Agents execute tasks, and E2B provides channel-scoped cloud computers.

## Source map

```text
backend/
  app/
    api/              REST endpoints, WebSockets, and authenticated file access
    band/             Band Human API bridge and enqueue-only agent listeners
    computer/         E2B lifecycle, observations, actions, desktop and app proxies
    persistence/      SQLModel models and database access
    runtime/          Deep Agent harness, internal helpers, skills, and tools
    tasks/            Durable queue, worker, approvals, schedules, and usage
    workspace/        Channels, teammates, policies, skills, events, and audit
    auth.py           Sessions, password hashing, and secret encryption
    config.py         Environment configuration
    documents.py      Document storage, extraction, and ingestion
    main.py           API process entry
    openai_provider.py Shared OpenAI configuration and task model snapshots
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
      settings/       OpenAI settings and connected-app management
      shared/         Reusable application presentation components
      workspace/      App shell, navigation, sign-in, and shared UI types
      ui/             shadcn primitives
      assistant-ui/   Presentational Tool UI elements and their utilities
    api.ts            Authenticated requests, uploads, and downloads
    store.ts          Workspace state and server-event updates
    types.ts          Shared API contracts
    app.css           Theme tokens and the sole application stylesheet
    App.tsx           Authentication and workspace bootstrap
    main.tsx          React entry
  public/             Static assets served without bundling
  scripts/            UI catalogue generation and production bundle checks
  tests/              Offline JavaScript and compiled-worker tests
docs/                 Repository architecture and supporting documentation
.github/workflows/    Automated backend and frontend checks
```

## Runtime boundaries

The API process handles authentication, permissions, messages, WebSockets, Band listeners, and desktop access. Incoming agent requests create durable tasks in the application database. Model reasoning runs only in the separate worker, which stores execution progress in LangGraph checkpoints and commits task results to the application database. The API publishes those results to connected clients.

Visible teammates have Band identities, roles, instructions, and persistent memory. Internal helpers are scoped to one task. Channel membership and policies are enforced on the server; frontend permission controls reflect those rules.

This deployment supports one API process and one worker on a shared local filesystem. The worker may run several independent tasks concurrently while maintaining order for each agent in each channel. Multiple API replicas, independent worker replicas, multi-host execution, and tenant isolation require architectural changes; see the README's deployment limits.

## Frontend boundaries

`App.tsx` handles sign-in and bootstrap. `components/workspace/Workspace.tsx` composes the member directory, conversations, modals, and computer panel. Domain components own API calls, polling, and interaction state. Shared workspace UI types live in `components/workspace/types.ts`; API request and response types remain in `src/types.ts`.

Use relative imports within a feature and `@/` imports across features or into `src/api.ts`, `src/store.ts`, and `src/types.ts`. Import components directly rather than through barrel files, so lazy-loaded interfaces, syntax grammars, maps, and diagram workers retain their current bundle boundaries.

`components/shared/` contains application presentation reused across features. `components/ui/` and `components/assistant-ui/` retain their existing layouts and installation workflows. Add theme and component styles only to `src/app.css`.

## Local state and generated files

The default application database, checkpoints, and document originals remain under `backend/` for compatibility. Dependencies, runtime state, secrets, test caches, builds, and QA output are ignored by Git. Reorganizing source must not relocate or delete live databases, checkpoints, encrypted credentials, or user files.

Keep `backend/uv.lock`, `frontend/package-lock.json`, and the generated runtime UI catalogue under version control. The catalogue is a committed contract checked by the frontend build, rather than disposable build output.
