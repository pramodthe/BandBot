# Providers & APIs

Admins configure independent workspace services from **Providers & APIs** in the settings rail. Human conversations work before optional services are configured. Band remains the messaging and agent-identity backbone; personal Codex, Claude, Cursor and OpenClaw connections keep their ownership rules.

## Rollout in three stages

1. Connection registry, encrypted revisions, additive migration and legacy task/document/computer bindings. Import is local, idempotent and serialized with API/worker startup. Existing defaults are preserved. Fresh installations import environment credentials as unverified connections and require verification/activation; unset services are explicitly disabled.
2. Native model adapters and independent computer, research, retrieval, document vision and image services. Shared offline contracts cover tool/vision verification and both computer providers.
3. Admin settings, activation preview, retirement, recovery controls and browser rollout. Routine changes are read from the shared database and require no restart.

## Add and activate a connection

1. Add a named connection. Configure exact model IDs from your account, not display labels. Compatible endpoints also require their context window. Credentials and configuration are encrypted using the stable `APP_SECRET`.
2. Save, then explicitly choose **Verify connection**. Model verification makes small billable vision/function-call requests and checks the tool response continuation and usage reporting. Configured image models generate a billable test image to prove actual image output. Authentication alone does not make a model selectable.
3. New computer accounts require the explicit billable isolated VM check. It checks Internet access, browser/desktop input, file writes and pause/resume, and journals ownership for cleanup. Read-only computer verification does not allocate a VM. An interrupted probe must be cleaned up before another check.
4. Select verified connections and models separately for reasoning, computers, research, document search, document analysis/scan transcription, image generation and integrations. Preview the change, then activate it. Agent LLM overrides are administered separately in the same screen. Existing imported agent/channel computer overrides remain unless explicitly replaced.

OpenAI and Gemini image models are independent of the reasoning model. Anthropic/Gemini/compatible agents never receive OpenAI Responses-only tool definitions. New model connections use the selected computer for portable Python calculations. Optional OpenAI hosted execution remains available to imported connections with its existing capability; it is not inferred for a newly selected model.

Compatible endpoints use standard Chat Completions only. Vendor-specific extensions require a dedicated adapter. Public endpoints require HTTPS. Local/private endpoints require an operator to set an exact origin in `PROVIDER_ENDPOINT_ALLOWLIST`, for example `http://127.0.0.1:11434`. Configure that endpoint connection with its full API base path, such as `/v1`. Redirects are disabled, all DNS results are checked, and requests pin the checked IP while retaining the original TLS hostname. Link-local/metadata addresses are never allowed. The allowlist is server configuration, not an admin UI permission grant.

## Switching and recovery

Every task and document-processing job pins its provider, model, runtime contract and connection revision in its creation transaction. Queued, running, approval-paused and retried work keeps those selections. Rotating a key creates a new revision; it does not rewrite historical credentials. A still-valid original revision can resume. If the original credentials no longer work, activate a verified replacement and explicitly start a linked new task.

There is no automatic failover. A blocking failure records a safe **Needs connection** reason and preserves the checkpoint. **Retry** uses the original binding. **New task with current providers** creates a separate checkpoint and links back to the source; current permissions and approval policies still apply.

Native model requests and image requests journal dispatch before contacting the provider. Confirmed model rejections can be retried. Completed model responses replay locally. A lost/uncertain response is not automatically sent again. Review provider billing/outcomes before explicitly starting another task. Interrupted verification without VM resources can be acknowledged separately; acknowledgement never verifies or activates the connection.

A computer belongs to its connection revision and original scope. A different account/revision/provider creates a separate destination with a distinct ownership namespace. It never transfers or deletes the original VM, durable disk, files or browser session. Tenki workspace sharing still excludes private chats. Historical computer bindings, operation locks, screenshot observation checks and human takeover remain enforced.

## Documents and resource ownership

Canonical originals, extracted text and durable application download/citation URLs are provider-neutral. Local search ranks permission-filtered text chunks with BM25. Scan transcription and full-document analysis use the independent vision model; calculation tools execute in the pinned cloud computer. Oversized full-document analysis fails with a batch/split instruction rather than silently claiming complete coverage.

Switching retrieval stages new indexes in the worker. The previous selection remains active until all ready text documents are indexed. Index transitions serialize with ingestion in the supported single-worker deployment. Uploads queued before activation retain their own job binding and also prepare the currently active retrieval index. Historical task searches can request their original connection's index; they do not use a new account's remote IDs. Permission/deletion checks run again after search or analysis.

Remote file, collection and index IDs retain their original account/revision. Upload intents and deterministic names support reconciliation after uncertain remote responses; uncertain uploads cannot be reset to a fresh upload by ordinary retry. Removing a document cleans its remote resources through their original owners. Empty remote collections are released only after their indexes are unused. Retired connections can explicitly release unused remote indexes after selections and unfinished work are reassigned; canonical files remain readable.

## Retention, authorization and operations

Keys are write-only. API responses and audits never include credentials, encryption envelopes or raw provider errors. Activation and key rotation use configuration revisions; stale writes fail instead of overwriting another admin. Retirement disallows new selections and retains existing dependencies. Removal is blocked by defaults, overrides, unfinished and retryable task checkpoints/document jobs, retained computers, remote resources or uncertain operations. Emergency revocation is separate and immediately blocks old revisions as well as new requests.

Continue running one API and one worker against the same database, checkpoint path and `APP_SECRET` on one shared local filesystem. Schema/import locks and computer operation locks cover those processes. Additional independent worker replicas remain unsupported.

The legacy `/api/settings/openai` API remains a compatibility wrapper over immutable revisions. It does not replace a selected Anthropic/Gemini default. Native accounting records provider, connection and revision; non-OpenAI managed usage uses qualified IDs such as `anthropic:claude-…` in `MODEL_PRICES` to avoid matching another provider's model rates.

Live cloud tests are opt-in from the admin screen; the ordinary regression suites use fakes and no service keys. Browser tests use a temporary database and ports 18019/18173, leaving a running local workspace on 8000 untouched. PostgreSQL CI covers the same provider contracts and migration.
