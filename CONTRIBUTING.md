# Contributing to BAND_BOTS

Read [AGENTS.md](AGENTS.md) for repository conventions and application invariants, [README.md](README.md) for setup and product behavior, and [the architecture guide](docs/architecture.md) for code placement. Report security issues privately as described in [SECURITY.md](SECURITY.md).

## Development

Use Python 3.12+, uv, and Node.js 22.12+. Install the locked dependencies from each package:

```sh
cd backend
uv sync --locked
cd ../frontend
npm ci
```

Copy `.env.example` to `.env` and run the API, worker, and Vite in separate terminals as described in the [README quickstart](README.md#quickstart). One API process and one worker must share the same database, checkpoint path, environment, and application secret.

## Making changes

- Keep frontend application components in their feature folders; shared presentation belongs in `components/shared/`. Use `@/` imports across features and relative imports within a feature. Import shared workspace UI types from `components/workspace/types.ts`.
- Keep `components/ui/` and `components/assistant-ui/` for their existing component systems. Application polling, API calls, and task controls belong in feature components.
- Keep agent execution in the backend worker. Preserve transactional task creation, checkpoint recovery, approvals, channel permissions, memory isolation, and computer take-over controls.
- Follow the existing code style and `.editorconfig`. Python code must pass `uv run ruff check` (a minimal lint for unused or undefined names and bare `except`); no formatter is configured.
- Add database schema changes as a new Alembic revision in `backend/alembic/versions/`; never edit a released revision.
- Keep credentials, databases, document storage, generated builds, dependencies, and local QA artifacts out of commits. Retain `.env.example` and both dependency lockfiles.

## Verification

Run from the indicated package directory:

```sh
# backend/
uv run ruff check
uv run pytest -q

# frontend/
npm test
npm run build
npm run e2e   # two-user browser suite; needs `npx playwright install chromium` once
```

Backend tests use isolated temporary databases and fake providers. Ordinary tests must not call live Band, OpenAI, Exa, E2B, or Composio services. Live integration checks need separately configured accounts and should use a dedicated test conversation.

For UI changes, exercise the affected flow in a browser at desktop and phone widths. Check console errors, loading and error states, overflow, keyboard access, and the rendered result. The production build checks the UI catalogue, TypeScript, script-size budgets, and compiled diagram worker.

CI runs on pushes to `main` and on pull requests: the backend suite, frontend tests and production build, PostgreSQL migration and workspace tests, the two-user browser suite, and a Docker image build.

## Pull requests

Before committing, run `git diff --check` and inspect the staged files. Use concise imperative commit subjects. Pull requests should explain the resulting behavior, link related issues, and include verification results and screenshots for UI changes; the pull request template lists the checks.
