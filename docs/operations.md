# Operations

This installation runs **one API process and one worker**. They share `DATABASE_URL`, `AGENT_CHECKPOINT_PATH`, `DOCUMENTS_PATH`, and `APP_SECRET`. Pointing `DATABASE_URL` at PostgreSQL does not add replicas. The event bus, Band listeners, and checkpoint lock stay on one machine. An uncertain Band send is never replayed during migration or restore.

## Application secret and sessions

`APP_SECRET` encrypts stored Band and provider credentials and signs session tokens. Do not rotate it as part of this upgrade. A new secret would make stored credentials unreadable. Signing and encryption still share this one secret; splitting them needs a separate migration.

Set `BAND_ENV=production` on a real deployment. Startup then refuses to run when `APP_SECRET` is missing, shorter than 32 characters, or one of `dev-secret-change-me`, `changeme`, `secret`, or `password`. It also refuses `RECOVERY_DELIVERY=memory`. The refusal does not print the secret.

The browser stores the session token in `localStorage` under `band-slack-token` and sends it as `Authorization: Bearer`. The API does not use a session cookie, so a third-party site cannot attach the token with a classic cross-site form post. Keep the token out of URLs other than the WebSocket query string the app opens itself.

Each sign-in inserts an `auth_session` row and puts that session id in the token. `POST /api/auth/logout` revokes the current session. `POST /api/auth/logout-all` revokes every session for the account, including after a password reset. The WebSocket rechecks the session about every five seconds and closes with `4401` when it is revoked or expired. The app then returns to the sign-in screen. Tokens issued before sessions existed have no session id and are rejected; those people sign in again. Encrypted Band and provider credentials stay valid because `APP_SECRET` is unchanged. Sessions expire after 30 days.

`AUTH_ATTEMPT_LIMIT` (default 20) and `AUTH_ATTEMPT_WINDOW_SECONDS` (default 600) cap signup attempts per client address and failed logins per address plus username. The lockout response is `Too many attempts. Try again later.` Login failures use `Wrong username or password` for an unknown username and a wrong password. Signup still returns 409 when the username is taken, because usernames are public handles.

## Account recovery

Recovery uses a verified email. `POST /api/account/recovery-email` stores the address as unverified and sends a code. `POST /api/auth/recovery/verify` marks that address verified. The code expires in 60 minutes and works once.

`POST /api/auth/recovery` always answers `If that account has a verified recovery email, a reset message is on its way.` Mail is sent only when that username has a verified address. `POST /api/auth/recovery/reset` accepts a code for 15 minutes, once. A successful reset revokes every session.

`RECOVERY_DELIVERY` is `none` (drop the message), `memory` (keep it in the API process for tests; refused in production), or `smtp`. `SMTP_URL` is a URL such as `smtp://user:password@mail.example:587`. The message body and code are not written to the log.

## People, blocks, and invitations

Contact requests, invitation decisions, cancellations, withdrawals, removals, and blocks publish `people_changed` only to the people involved. The event has no channel name or message. Other clients learn nothing from it. The app reloads People from the server when that event arrives, and again after a reconnect. The People dialog also polls while it is open.

The requester can cancel a pending contact request. The inviter can withdraw a pending channel invitation. A later accept is a no-op and does not create a contact or a membership. Repeating a decision on a request that is no longer pending is safe.

Removing a contact deletes the contact row and cancels pending requests between those two people. Shared channels, memberships, and message history stay. A new direct message needs a newly accepted request.

Blocking is recorded in `contact_block`. Either person's block stops new contact requests, new human direct messages, and new channel invitations between them. It does not remove them from a shared channel and does not delete history. An existing human direct message stays in the list with its history, and the composer is replaced with a notice that messaging is unavailable. A shared channel keeps its composer. Unblock removes only that person's block row.

## Worker and provider status

`GET /api/health` is public and reports only that the API is up and whether Band is configured and online. It does not include error text or credentials.

`GET /api/status` requires a signed-in user. It reports API liveness, human chat availability, worker state (`starting`, `ready`, `busy`, or `unavailable`), and each connected runtime's state (`ready`, `sign_in_required`, or `unavailable`). `action: "reconnect"` is included only for the person who created that provider connection. Other people see that sign-in is required and do not get the action. Details are short state codes. Provider secrets and raw provider errors are not included. The worker does not call a model to prove it is healthy.

The worker writes `worker_heartbeat` on each queue loop, including while idle. A beat older than `WORKER_HEARTBEAT_STALE_SECONDS` (default 20) is stale. For `WORKER_STARTUP_GRACE_SECONDS` (default 15) after the API starts, a missing beat is `starting` rather than `unavailable`. The channel shows **Worker unavailable** when the heartbeat is stale after that grace. Human messages still send. Messages to agents stay queued and run when the worker returns, without a second task or a second Band delivery. Start the worker with `python -m app.worker` from `backend/`, using the same environment as the API. Only one worker may hold the checkpoint lock.

When an authorized owner sees **Reconnect provider**, they open Add agents and sign in to that provider again. The app does not invent a provider error body.

## Database migrations, PostgreSQL, and restore

Schema changes ship as Alembic revisions in `backend/alembic/versions`. From `backend/`:

```bash
uv run alembic upgrade head
```

The API calls the same upgrade on startup. A database that already has the `user` table and no `alembic_version` table is stamped at `0001_baseline` and is not rebuilt. Later revisions add missing tables. Fresh databases run every revision. Production startup does not apply unversioned column changes.

`DATABASE_URL` may be SQLite (`sqlite:////absolute/path.db`) or PostgreSQL (`postgresql://…` or `postgresql+psycopg://…`). Install the locked dependencies so `psycopg` is present. `AGENT_CHECKPOINT_PATH` may be a SQLite file or a PostgreSQL URL; a hosted deploy without a durable disk can point it at the same database as `DATABASE_URL`. `DOCUMENTS_PATH` may be a directory or the value `database`, which stores original file bytes in the application database.

Copy an existing SQLite installation by leaving its file in place and starting the new process against that `DATABASE_URL`. The stamp-then-upgrade path keeps users, contacts, requests, memberships, Band ownership, encrypted connections, tasks, and audit rows. Take a backup first.

`app.persistence.migrate.backup_workspace` copies three things into a directory you choose:

- the SQLite application database, when `DATABASE_URL` is SQLite, via the SQLite backup API
- the checkpoint database, when that file exists
- the documents directory

For PostgreSQL, dump the application database with `pg_dump` and copy the checkpoint file and documents directory beside it. Restore by stopping the API and worker, replacing those three stores from the same moment, and starting one API and one worker. Do not start a second worker against the restored checkpoint. Do not change `APP_SECRET` during restore. Do not resend tasks whose Band delivery status is `unknown`.

Roll back a bad release by stopping the processes and putting the backed-up application database, checkpoint file, and documents directory back. A PostgreSQL restore uses `pg_restore` or a plain SQL dump from that same moment. Do not generate a new `APP_SECRET` to roll back.

## Browser regression

From `frontend/`:

```bash
npm run e2e
```

Playwright starts the API, the worker, and Vite with a temporary database, checkpoint file, and documents directory. `E2E_FAKE_AGENT=1` is set only by that script, and the fake reply is ignored when `OPENAI_API_KEY` or `BAND_USER_API_KEY` is set. No Band, OpenAI, Claude, Cursor, E2B, or OpenClaw credentials are required. The same command is the GitHub Actions browser job. It covers two signed-in browsers, contact consent, direct messages, invitations, a deterministic agent reply, authorization checks, the external-agent error screen, reconnect, and retry, on a desktop viewport and a compact viewport. Failure screenshots and traces stay in `frontend/test-results/` and `frontend/playwright-report/`, which are gitignored.
