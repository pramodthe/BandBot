# Contributing to BAND_BOTS

Read [AGENTS.md](AGENTS.md) for repository conventions and application invariants, [README.md](README.md) for setup and product behavior, and [the architecture guide](docs/architecture.md) for code placement.

## Development

Use Python 3.12+, uv, and Node 20+. Install the locked dependencies from each package:

```sh
cd backend
uv sync --locked
cd ../frontend
npm ci
```

Copy `.env.example` to `.env` and configure the required services as described in the README. Run the API, worker, and Vite in separate terminals using the README commands. One API process and one worker must share the same database, checkpoint path, environment, and application secret.

## Making changes

- Keep frontend application components in their feature folders; shared presentation belongs in `components/shared/`. Use `@/` imports across features and relative imports within a feature. Import shared workspace UI types from `components/workspace/types.ts`.
- Keep `components/ui/` and `components/assistant-ui/` for their existing component systems. Application polling, API calls, and task controls belong in feature components.
- Keep agent execution in the backend worker. Preserve transactional task creation, checkpoint recovery, approvals, channel permissions, memory isolation, and computer take-over controls.
- Follow the existing code style and `.editorconfig`. The repository does not require a formatter or linter.
- Keep credentials, databases, document storage, generated builds, dependencies, and local QA artifacts out of commits. Retain `.env.example` and both dependency lockfiles.

## Verification

Run from the indicated package directory:

```sh
# backend/
uv run pytest -q

# frontend/
npm test
npm run build
```

Backend tests use isolated temporary databases and fake providers. Ordinary tests must not call live Band, OpenAI, Exa, E2B, or Composio services. Live integration checks need separately configured accounts and should use a dedicated test conversation.

For UI changes, exercise the affected flow in a browser at desktop and phone widths. Check console errors, loading and error states, overflow, keyboard access, and the rendered result. The production build checks the UI catalogue, TypeScript, script-size budgets, and compiled diagram worker.

CI runs the backend tests and frontend tests/build on pushes to `main` and pull requests. Before committing, run `git diff --check` and inspect the staged files. Use concise imperative commit subjects; PR descriptions should explain the resulting behavior and include verification results and screenshots for UI changes.
