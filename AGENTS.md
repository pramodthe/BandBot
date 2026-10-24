# BAND_BOTS Repository Guidelines

## Product Intent

BAND_BOTS is a shared workspace where people and AI teammates get work done together. "Grok Bot meets Slack" is the informal product analogy: Slack-style channels and conversation combined with persistent agents that use tools, remember context, delegate work, and operate cloud computers.

The product promise is: **A shared workspace where your team and AI teammates get work done together.** The implementation uses Band for messaging and agent identity, OpenAI models with Deep Agents for execution, and E2B or Tenki for cloud computers.

When changing the product:

- Treat Kylon and Openbot as references for useful UX patterns. Keep BAND_BOTS's product identity and its Band, Deep Agents, and E2B architecture.
- Keep people and agents working in the same channel context. Visible teammates have persistent identities, roles, and memory; internal helpers are scoped to a task.
- Make task ownership, progress, blockers, approval requests, and finished results easy to find.
- Keep agent hand-offs and chatter readable so collaboration does not overwhelm the conversation.
- Preserve human controls for approvals, cancellation, resume, and computer take-over, including the channel's permission policies.
- Keep setup and implementation details out of everyday task flows unless the user needs them to make a decision. Agent computers should be discoverable from the agent profile and chat, with clear channel scope, live viewing, files, activity, and human take-over controls.

## Band integration policy: local people, per-user agent management

- User signup, login, contacts, invitations, memberships, channels, human direct messages, and live human chat belong to our database and WebSocket service. Band credentials are optional at signup. Never require Band for local human collaboration.
- Each user may save their own **Band user API key** (`band_u_…`) at signup or in People. Validate it against Band, encrypt it with `APP_SECRET`, and use it only to create and manage that user's native Band agents. Obtain this key in Band Settings → API Keys.
- Band names native agent registration/deletion endpoints **Human API** (`human_api_agents`, `/api/v1/me/agents`). These agent-management calls ARE supported and required. Do not confuse this naming with implementing Band-hosted human accounts or chat. Do not remove agent management when human chat integration is excluded.
- Newly registered agents receive separate **Agent API keys** (`band_a_…`). Validate and encrypt these keys; each native agent retains its own identity, profile, instructions, memory, tasks, approvals, and computer scope. Its REST/SDK/WebSocket transport uses its agent key. Reasoning runs only in the worker.
- Preserve importing existing native agents and existing saved Agent API transport connections. An Agent API connection alone cannot register new native identities; compatibility app teammates can share that transport with separate app profiles. Removing an imported identity or a shared transport teammate must not delete its native Band identity.
- Never use a workspace `.env` key, another user's key, or another account as a fallback. User A's agent-management calls must use A's saved user key, and B's must use B's. Reconnects must retain ownership. Validate ownership before deleting a managed native agent.
- Native Band human profile/chat synchronization, impersonating app humans on Band, and Human API room/message polling are not implemented. Native human-chat integration remains **coming soon**, separately from supported native agent management.
- Preserve historical rooms and messages; do not revive old human-chat transport. Native Agent API rooms remain pinned to their original connection. Model-provider and cloud-computer authentication are separate from Band authentication.

## Project Structure & Architecture

- `backend/app/main.py`: ASGI entry; `api/http.py` defines REST/WebSocket endpoints and serves the built UI.
- `backend/app/workspace/`: channels, messages, agent profiles, roles, permissions, events, and activity logging. `service.py` coordinates the workspace.
- `backend/app/band/`: `platform.py` uses the per-user Agent API; `platform_listener.py` and `listener.py` receive agent turns and enqueue work. The scoped account client supports native agent management; human chat remains local.
- `backend/app/tasks/`: durable queue, worker, hand-offs, teammate creation requests, approvals, schedules, streaming drafts, usage, and budgets. `backend/app/worker.py` is the worker process entry.
- `backend/app/runtime/`: the per-task Deep Agent harness, internal helpers, and tools for memory, research, teammates, approvals, integrations, and computers.
- `backend/app/computer/`: E2B and Tenki desktop lifecycle, durable disks, observations, actions, and files.
- `backend/app/persistence/`: SQLModel database models. Authentication and configuration live in `backend/app/auth.py` and `backend/app/config.py`.
- `backend/tests/`: offline pytest regression tests.
- `frontend/src/`: React/TypeScript UI; application components are grouped by feature in `components/{agents,chat,computer,documents,generated,settings,shared,workspace}/`. Shared API types live in `types.ts`, API helpers in `api.ts`, state in `store.ts`, and shared workspace UI types in `components/workspace/types.ts`. See `docs/architecture.md` for placement guidance.
- `frontend/public/`: static assets; `frontend/dist/`: generated production UI.

The API and Band listeners enqueue tasks; model reasoning runs in a separate worker. The worker persists execution in LangGraph checkpoints and publishes results through the application database. See `README.md` for configuration and deployment limits, and `CLAUDE.md` for detailed implementation notes and Tool UI installation steps.

## Build, Test, and Development Commands

Use Python 3.12+ with uv and Node 20+. Run commands from the indicated directory.

| Directory | Command | Purpose |
| --- | --- | --- |
| `backend/` | `uv sync --locked` | Install backend and development dependencies. |
| `backend/` | `uv run uvicorn app.main:app --reload --port 8000` | Start the API and Band listeners. |
| `backend/` | `uv run python -m app.worker` | Start the separate worker required for agents to answer. |
| `backend/` | `uv run pytest` | Run the offline backend suite. |
| `frontend/` | `npm ci` | Install frontend dependencies from the lockfile. |
| `frontend/` | `npm run dev` | Start Vite at port 5173; `/api` and `/ws` proxy to port 8000. |
| `frontend/` | `npm run build` | Run TypeScript checks and build production assets. |
| `frontend/` | `npm test` | Run offline worker and syntax-loader regression tests with Node's built-in runner. |

For local development, run the API, worker, and Vite in separate terminals. FastAPI can serve `frontend/dist/` at port 8000 after a production build.

## Coding Style & UI Conventions

- Follow existing code. Python uses four-space indentation, type hints, snake_case functions/modules, and PascalCase classes.
- TypeScript/TSX uses two-space indentation, double quotes, semicolons, camelCase variables, and PascalCase component filenames. Keep shared API types in `types.ts`.
- Keep application components in their feature folders. Use relative imports within a feature and `@/` imports across features; keep `components/ui/` and `components/assistant-ui/` in their established locations. Follow `.editorconfig` and preserve LF line endings.
- The UI follows a Kylon-inspired white workspace: a 52px navigation rail, member/conversation directory, circular avatars, and spacious profile panels. Use Geist, Tailwind 4 utilities, and semantic theme tokens in `frontend/src/app.css`, the only stylesheet. Use `narrow:` for compact panels at 900px and `sm` utilities for phone navigation.
- Reuse shadcn primitives in `components/ui/`; add missing ones with `npx shadcn@latest add <name>` from `frontend/`.
- Tool UI elements live in `components/assistant-ui/elements/`, with shared helpers in `components/assistant-ui/utils/`. Add them with `npx shadcn@latest add @assistant-ui/elements-<name> --yes`, then follow the move, import, duplicate-file, and stylesheet cleanup steps in `CLAUDE.md`.
- Keep polling, API calls, mention matching, and scroll behavior in application components; Tool UI elements remain presentational.
- No dedicated formatter or linter is configured. The frontend build checks unused declarations and switch fallthrough.

## Application Invariants

- Preserve atomic message/task creation, task deduplication, ordering per agent per channel, and checkpoint recovery. Keep model execution out of the API process.
- Keep visible Band teammates distinct from internal helpers. Preserve bounded hand-offs and replay-safe teammate creation.
- Preserve the application's approval flow for external actions, MCP approval policies, and connected-app access restrictions. Approval pauses must release the worker slot and resume from checkpoints.
- Tell the parent agent and helpers which services are disabled in the task's pinned bindings. A disabled optional service must produce an actionable tool limitation rather than aborting the model turn. Configured connection failures, revocations, permission denials, and uncertain paid actions must retain their recovery safeguards. Never enable another service or execute on the host as a fallback.
- Enforce channel membership and role/policy checks on the server. Frontend controls reflect these permissions but do not replace them. This is one shared workspace; channel access is not tenant isolation.
- Preserve agent/channel memory isolation, audit secret masking, token accounting, and daily budget enforcement.
- Keep skill management admin-only. Installed skills and supporting files are read-only workflow guidance for the agent and its task helpers; imports must stay bounded and must never extract or execute files on the host or grant tools. Clear skill assignments when an agent is deleted.
- E2B computers remain isolated per agent Band identity and channel. Tenki supports `TENKI_COMPUTER_SHARING=workspace`: one workspace-owned computer and durable disk shared across agents in regular channels. Private conversations retain separate computers; saved agent memory and approvals retain their scopes. Preserve pinned historical task bindings. Serialize computer operations across API/worker processes, apply human take-over to every agent using the computer, and never pause it while another task is using it. Verify ownership before resuming; keep ingress private and provider credentials server-side. Pause preserves files and browser state; do not kill a VM to detach its SDK client. Removing one agent must never retire the shared workspace disk.
- Treat uncertain Band sends as uncertain; never blindly resend them. Preserve screenshot observation checks and human take-over controls for computer input.

## Testing Guidelines

- Name Python tests `test_*.py` with descriptive `test_*` functions. Use FastAPI's `TestClient` for HTTP/WebSocket behavior and add regression coverage for changed backend behavior.
- Preserve offline isolation: `backend/tests/conftest.py` selects a temporary SQLite database and clears external service keys and model pricing. Configuration is read at import time, so environment overrides must precede application imports.
- Use the existing scripted model for harness/tool tests and fake E2B SDK for computer tests. Ordinary tests must not call live Band, OpenAI, Exa, E2B, or Composio services.
- No coverage threshold is configured. Frontend loader regression tests use Node's built-in runner via `npm test`. Verify UI changes with `npm run build` and browser checks of affected interactions and responsive layouts. The production build also enforces 500 kB page-script and 2 MB on-demand diagram-worker budgets; keep Vite's warning limit unchanged.
- Run checks appropriate to the change and report what passed, failed, or could not run. Documentation-only edits need a diff/content check; application tests are unnecessary unless behavior changes.

## Security & Configuration

Copy `.env.example` to the root `.env` and configure `APP_SECRET` and any optional service keys. Users optionally enter their Band user API key during signup or in People to manage native agents; model providers can be configured in Providers & APIs. Optional services include Exa research, Composio app connections, E2B computers, and MCP servers; consult `README.md` for their environment variables and configuration.

Never commit credentials, local `.env` files, databases, checkpoints, or secret-bearing MCP configuration. Keep `APP_SECRET` stable because it protects stored agent keys. Keep secrets and raw provider errors out of user-visible messages and audit details.

When adding `.env` after the first startup, preserve the original `APP_SECRET` or explicitly migrate existing encrypted records. Provider configurations are encrypted even when they contain empty local/MCP settings; an installation with no user accounts can already contain encrypted data. Do not silently change the secret or add a runtime decryption fallback.

Run **one API process and one worker** against the same application database, checkpoint path, environment, and application secret on a shared local filesystem. The default files are `backend/band_slack.db` and `backend/agent_checkpoints.db`. Independent worker replicas and multi-host execution require architectural changes.

## Cursor Cloud specific instructions

Python 3.12 and Node.js 22 are already on the image. Environment install puts `uv` on `/usr/local/bin` (login shells used for `install` and `start` do not load `~/.bashrc`), then runs `uv sync --locked` in `backend/` and `npm ci` in `frontend/`.

On every boot the start script opens three tmux sessions: `api` (`uv run uvicorn app.main:app --host 0.0.0.0 --port 8000` in `backend/`), `worker` (`uv run python -m app.worker` in `backend/`), and `vite` (`npm run dev -- --host 0.0.0.0 --port 5173` in `frontend/`). A second run leaves sessions that are already up. Readiness is `GET /api/health` on port 8000 and an HTTP response from port 5173.

In development and tests, `APP_SECRET` falls back to `dev-secret-change-me` when unset, which is enough for local signup and sessions. Production (`BAND_ENV=production`) refuses this fallback. New accounts need no Band key. Users can chat locally and add their own Band user API key in People when they want to create native agents. The API health endpoint stays independent of individual Band connections; authenticated bootstrap reports the user's Band state. There is no workspace Band key fallback or automatic Band registration of Master. Offline tests use fake Agent API clients and listeners. Do not commit `.env`, `backend/band_slack.db`, or `backend/agent_checkpoints.db`.

## Commit & Pull Request Guidelines

Use concise, imperative commit subjects. PRs should describe the concrete behavior change, include verification commands/results, link relevant issues, and provide screenshots for UI changes. Keep unrelated local changes intact and update documentation when setup, configuration, or behavior changes.
