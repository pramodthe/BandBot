## What changes

<!-- The behavior this PR changes and why. Link related issues. -->

## Verification

<!-- Commands you ran and their results. -->

- [ ] `uv run ruff check` and `uv run pytest -q` in `backend/`
- [ ] `npm test` and `npm run build` in `frontend/`
- [ ] Browser check at desktop and phone widths (UI changes; attach screenshots)
- [ ] `git diff --check` is clean

## Checklist

- [ ] Documentation updated for setup, configuration, or behavior changes
- [ ] No credentials, `.env` files, databases, checkpoints, or local QA output committed
- [ ] Application invariants in `AGENTS.md` preserved (approvals, permissions, checkpoint recovery, memory isolation)
