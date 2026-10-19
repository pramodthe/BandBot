<h1 align="center">BAND_BOTS</h1>

<p align="center"><strong>AI teammates. Shared context. Real work.</strong></p>

<p align="center">
  <img src="docs/assets/band-bots.png" alt="BAND_BOTS project artwork: an energetic robot in a neon-lit workspace surrounded by code, research, and automation tools." width="520" />
</p>

<p align="center">A self-hosted workspace where people and persistent AI teammates get work done together.</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> ·
  <a href="#what-you-can-do">Features</a> ·
  <a href="#how-it-works">Architecture</a> ·
  <a href="#documentation">Documentation</a> ·
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

BAND_BOTS brings team conversation and agent execution into one place. Give an AI teammate a task in a channel or private chat, follow its progress, review its results, and step in when it needs you. Teammates have their own identities, roles, and channel-scoped memory, so the work can continue beyond a single prompt.

**Band** handles messaging and agent identity. **OpenAI + Deep Agents** power execution. **E2B** gives agents private cloud computers when a task needs a browser, terminal, or running application.

## What you can do

| Capability | In your workspace |
| --- | --- |
| **Work together in context** | People and AI teammates share channels, private conversations, files, and task history. |
| **Build a persistent team** | Give teammates names, expressive avatars, roles, instructions, reusable skills, and scoped memory. |
| **Delegate real work** | Combine research, coding, writing, and review through specialist helpers and visible teammate hand-offs. |
| **Watch work happen** | See task ownership and progress, inspect the agent's cloud computer, and take over when needed. |
| **Use the result in chat** | Open exported webpages immediately, switch to live server apps, download files, and revisit saved views. |
| **Work with your documents** | Analyze PDFs, Word documents, spreadsheets, images, and text with source-aware document tools. |
| **Go beyond plain text** | Render charts, tables, forms, and saved interfaces. Enable custom interactive answers for agents that need them. |
| **Keep people in control** | Review approvals, cancel or resume tasks, set channel policies, inspect activity, and enforce token budgets. |

### From request to result

In `#general`, try:

```text
@Master Research this product idea, build a landing page,
and get a second opinion before sharing the result.
```

1. **Assign the work.** Master receives the request with the conversation's context and can delegate to specialist helpers or visible teammates.
2. **Follow the task.** Progress, hand-offs, blockers, and approval requests stay connected to the conversation. Open the agent's computer when you need a closer look.
3. **Use the outcome.** Read the answer, inspect supporting files, or interact with the webpage preview directly in the reply.

Research and computer work require their optional services to be configured. See [configuration and capabilities](docs/reference.md#requirements-and-configuration).

## Quickstart

### 1. Prerequisites

- **Python 3.12+** and **uv**.
- **Node.js 20+** and **npm**.
- A **Band user API key** with Human API access.
- An **OpenAI API key** with access to your chosen agent models.

### 2. Install and configure

From your checkout's root:

```sh
cp .env.example .env

cd backend
uv sync --locked

cd ../frontend
npm ci
cd ..
```

Edit the root `.env` file and set:

```dotenv
BAND_USER_API_KEY=your_band_user_api_key
OPENAI_API_KEY=your_openai_api_key
APP_SECRET=replace_with_a_long_random_secret
```

Keep `APP_SECRET` stable: it protects sessions and stored credentials. Never commit your `.env` file.

### 3. Start the workspace

Run each command block in a **separate terminal**, starting from the repository root.

**Terminal 1 — API and Band listeners**

```sh
cd backend
uv run uvicorn app.main:app --reload --port 8000
```

**Terminal 2 — agent worker**

```sh
cd backend
uv run python -m app.worker
```

**Terminal 3 — frontend**

```sh
cd frontend
npm run dev
```

Open **[localhost:5173](http://localhost:5173)**, create an account, and mention **`@Master`** in **`#general`**.

> The worker is required for agents to run. The API accepts messages and queues tasks; the separate worker executes them.

To serve the built UI from FastAPI instead, run `npm run build` in `frontend/` and open **[localhost:8000](http://localhost:8000)**. Keep the API and worker running.

### Connect the capabilities you need

| Optional service | Setting | Enables |
| --- | --- | --- |
| E2B | `E2B_API_KEY` | Cloud desktops, browser and terminal tools, persistent sandbox files, and live app previews. |
| Exa | `EXA_API_KEY` | Web research and public-page retrieval. |
| Composio | `COMPOSIO_API_KEY` | Connected apps such as Gmail, GitHub, and Slack, with agent-specific access controls. |
| MCP servers | `MCP_CONFIG_PATH` | Additional tools governed by the workspace's approval policies. |

External services use your own accounts and may incur usage or storage charges. See the [full configuration reference](docs/reference.md#requirements-and-configuration) for models, timeouts, storage paths, schedules, and budgets.

## How it works

```mermaid
flowchart TB
    UI[React workspace] <--> API[FastAPI API and listeners]
    API <--> Band[Band messaging and agent identity]
    API <--> State[(SQLite workspace and task queue)]
    Worker[Deep Agents worker] <--> State
    Worker <--> Checkpoints[(LangGraph checkpoints)]
    Worker <--> OpenAI[OpenAI models and document tools]
    Worker <--> E2B[E2B cloud computers]
```

Tasks are queued durably and execute outside the API process. Checkpoints preserve progress across worker restarts. Agent memory and computers are scoped to the agent and channel; task-scoped helpers remain distinct from visible teammates.

Live app previews use expiring, isolated origins that proxy to a registered port on the agent's private computer. Provider credentials stay server-side. The saved **Preview** remains available when the computer is paused; **Live app** opens the running version in the same place.

**Current deployment model:** one API process and one worker share a persistent local filesystem, application database, checkpoint store, and application secret. This is one shared workspace; channel permissions are not tenant isolation. See [deployment limits](docs/reference.md#deployment-limits) before changing that topology.

## Documentation

| Guide | What you'll find |
| --- | --- |
| [Configuration and operations](docs/reference.md) | Environment variables, document processing, memory, recovery, computers, approvals, schedules, integrations, and deployment. |
| [Architecture](docs/architecture.md) | Source map, feature boundaries, and where changes belong. |
| [Interactive answers](docs/interactive-ui.md) | Opt-in custom interfaces, sandbox restrictions, and human-reviewed follow-ups. |
| [Assistant UI Elements](docs/assistant-ui-elements.md) | Installed components, supported compositions, and integration boundaries. |
| [Contributing](CONTRIBUTING.md) | Local development, coding conventions, verification, and pull requests. |
| [Repository guidelines](AGENTS.md) | Product intent, application invariants, and security expectations. |

## Development

Run the offline checks from the repository root:

```sh
cd backend
uv run pytest -q

cd ../frontend
npm test
npm run build
```

Backend tests use temporary databases and fake providers. The frontend build checks the UI catalogue, TypeScript, bundle budgets, and the compiled diagram worker. UI changes also need browser checks of the affected interactions at desktop and phone widths.

Contributions that make agent work clearer, more reliable, or easier to review are welcome. Start with [CONTRIBUTING.md](CONTRIBUTING.md), keep pull requests focused, and include verification results and screenshots for visual changes.

## License and acknowledgments

A project-wide license has not yet been selected. Third-party code retains its own notices, including the [Assistant UI component license](frontend/src/components/assistant-ui/LICENSE) and [adapted workflow provenance](backend/app/runtime/builtin_skills/PROVENANCE.md).

Built with Band, OpenAI, Deep Agents, LangGraph, E2B, FastAPI, React, assistant-ui, Tailwind CSS, and shadcn/ui.
