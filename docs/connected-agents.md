# Connected agents

**Add agents** offers Create agent, Codex, Claude Agent, Cursor, and OpenClaw. Signed-in users can create native Band identities using their own saved Band user key, use an existing Agent API transport connection, or import a separate native agent key. Account-scoped agent management is supported; native Band human-chat synchronization is coming soon. Provider connections belong to the person who authenticated them, are encrypted with `APP_SECRET`, and are never sent back to the browser. Removing a connection requires first removing its agents. API authentication checks are read-only and do not run inference; the first task verifies model access and billing.

All four connected runtimes supply the next reasoning step to the existing Deep Agents worker. The app owns conversation checkpoints, channel context, memory, skills, helpers, tools, E2B computers, approvals, cancellation, and budgets. Provider native execution tools stay disabled. This mode does not import personal desktop chats, remote memories, or vendor plugins. Responses appear at model-step boundaries, rather than streaming individual tokens. An absent token-usage report fails the task instead of silently treating usage as free. Unknown model prices remain unknown; set `MODEL_PRICES` for monetary budgets as described in the main configuration docs.

## Band adapters and this application's execution model

Band's [SDK overview](https://docs.band.ai/integrations/sdks/overview) separates platform connectivity from agent execution. Its direct [Codex adapter](https://docs.band.ai/integrations/sdks/tutorials/codex) runs Codex app-server and maps rooms to vendor threads; its [Claude SDK adapter](https://docs.band.ai/integrations/sdks/tutorials/claude-sdk) manages per-room Claude clients and injects Band tools through MCP. The [ACP overview](https://docs.band.ai/integrations/sdks/tutorials/acp-overview) distinguishes an editor connecting to Band (ACP Server) from a Band participant backed by an external agent (ACP Client Adapter). The latter supports Cursor through the documented `cursor_login` authentication method and `CURSOR_API_KEY` environment variable.

Those are supported alternatives for standalone Band participants. They are backend processes, not frontend authentication plugins. Codex's direct adapter can reuse an existing local `codex login` session; Claude's tutorial requires an Anthropic API key. Neither tutorial implements this web app's multi-user credential storage or account-connection screens.

This application deliberately uses `QueuedAgentAdapter` in `backend/app/band/listener.py` to durably enqueue messages, then invokes provider runtimes through `ConnectedRuntimeModel` in the separate worker. It does **not** currently instantiate Band's `CodexAdapter`, `ClaudeSDKAdapter`, or `ACPClientAdapter`. Replacing the queued adapter directly would move reasoning into the listener process and bypass the application's checkpointed tool execution unless those responsibilities were explicitly integrated. Native adapter support would need a worker-owned lifecycle, channel-bound tool wrappers, durable approval mapping, usage persistence, and replay-safe delivery before it could replace the existing bridge.

If the connection screen reports that the server returned a webpage instead of an API response, restart **both** the API and worker and reload the page. This indicates that a newer frontend is talking to an old backend without the connection routes. Unknown `/api/*` routes now return JSON 404 responses instead of the SPA document, and the connection picker rejects malformed data with a retry action.

## Codex

1. Install Codex on the API/worker host as described in the README. Open the local app using `localhost` or `127.0.0.1` from a browser on that same computer.
2. Select **Add agents → Codex → Sign in with ChatGPT**. The API starts Codex app-server with an empty, separate credential home and asks it to start its documented browser login. Complete the OpenAI authorization in the opened window; if a popup was blocked, use **Open sign-in**. Codex's callback listener requires port 1455 to be free. Only one login can run at a time; cancelling or closing the connection screen stops it.
3. After authentication, the app reads the account's model catalog, stores the Codex credential file encrypted, and selects its default model. Choose **Add Codex** to create its Band identity. Optional display-name and model changes are under Connection options. There is no custom character, specialty, or instruction form.

The worker restores this connection's session into an isolated temporary home for each reasoning step. Codex manages refresh; the app persists the updated credential file encrypted, serializing steps for that account to avoid concurrent refresh-token rotation. It never reads the host's personal Codex login. **Use an OpenAI API key instead** remains available for API billing. The native browser flow needs no `CODEX_CHATGPT_SIGN_IN` flag; that flag controls a separate optional Sign in with ChatGPT client flow.

Reference: [Codex app-server authentication](https://developers.openai.com/codex/app-server).

## Claude Agent

Select **Add agents → Claude Agent**, connect an Anthropic API key, then choose **Add Claude Agent**. A supported model and display name are selected automatically and can be changed under Connection options. The official Claude Agent SDK runs in the worker. Anthropic's SDK documentation requires API-key authentication for third-party products unless Anthropic has approved subscription login; this application does not offer a claude.ai subscription redirect.

Reference: [Claude Agent SDK authentication](https://code.claude.com/docs/en/agent-sdk/overview).

## Cursor

1. Run `uv sync --locked` in `backend/` on the API/worker host. The official `cursor-sdk` wheel includes its platform-specific bridge; installing the Cursor editor or CLI separately is unnecessary.
2. Create a **user API key** in [Cursor Dashboard](https://cursor.com/dashboard), or a supported service-account API key. Team Admin API keys are not supported by the SDK. This integration does not use the CLI browser-login flow or offer a third-party OAuth redirect.
3. Select **Add agents → Cursor**, enter a connection name and API key, and choose **Connect account**. The SDK reads account identity and available models without starting an agent run.
4. Choose **Add Cursor** and optionally add it to the current channel. A supported model and display name are selected automatically; adjust them under Connection options if needed.

The app launches each step in a clean temporary home with `tools=[]`, empty MCP/subagent definitions, and no personal settings. The structured response can request app tools, which the checkpointed worker executes under normal policies. SDK Router model `auto-smart` requires parameters this UI does not yet expose; choose another catalog model. SDK usage is billed to the connected Cursor account under its applicable plan. This embedded local SDK session is not a Cursor cloud agent or an existing editor chat.

References: [Cursor Python SDK](https://cursor.com/docs/sdk/python), [Cursor ACP and CLI authentication](https://cursor.com/docs/cli/acp), [Band's alternative Cursor ACP adapter](https://docs.band.ai/integrations/sdks/tutorials/acp-client).

## OpenClaw

This version uses OpenClaw's documented server-to-server HTTP Gateway token/password authentication. It does not implement browser device pairing or attach an unrestricted remote agent. A dedicated Gateway agent supplies reasoning; requested actions execute through the app. OpenClaw's Gateway bearer credential carries operator authority: keep it server-side and use a Gateway reserved for this integration on private ingress.

### Server and Gateway setup

1. Run an OpenClaw Gateway you administer. Use loopback on the same host or a private HTTPS endpoint accessible from both the API and worker. Browser `localhost` is not the user's machine when the app is hosted.
2. Create a dedicated agent with an explicit backend model and `tools.deny: ["*"]`. Give it a fresh workspace directory and reserve it for this bridge; do not reuse an agent with personal memory, other channel connections, lifecycle hooks, or another client's active session. Deny-all applies to its native tools; app tools are requested through the structured text response.
3. Enable the Gateway's Chat Completions endpoint. Permit the read-only `gateway` tool through HTTP so the app can inspect the `agents.list` configuration subtree. Its default control agent must be allowed to invoke that tool. The application only requests `config.get`; it never reconfigures or restarts the Gateway.
4. Add the exact Gateway base URL to `OPENCLAW_GATEWAY_URLS` in the app's root `.env`, then restart the API and worker. Multiple URLs are comma-separated. Paths, embedded credentials, queries, redirects, and arbitrary frontend URLs are not accepted. Plain HTTP is accepted only for explicitly configured loopback addresses.

Example Gateway configuration fragment (merge into your existing configuration; keep provider credentials separate):

```json
{
  "gateway": {
    "bind": "loopback",
    "auth": { "mode": "token", "token": "REPLACE_WITH_A_STRONG_SECRET" },
    "http": { "endpoints": { "chatCompletions": { "enabled": true } } },
    "tools": { "allow": ["gateway"] }
  },
  "agents": {
    "list": [
      {
        "id": "control",
        "default": true,
        "model": "openai/YOUR_MODEL_ID",
        "workspace": "/srv/openclaw/control",
        "tools": { "allow": ["gateway"] }
      },
      {
        "id": "band",
        "model": "openai/YOUR_MODEL_ID",
        "workspace": "/srv/openclaw/band",
        "tools": { "deny": ["*"] }
      }
    ]
  }
}
```

Application `.env`:

```dotenv
OPENCLAW_GATEWAY_URLS=http://127.0.0.1:18789
```

### Connect from the app

Select **Add agents → OpenClaw**, name the connection, select an approved Gateway, enter the dedicated agent ID (`band` in the example), and supply the Gateway token or password. Choose **Connect Gateway**, then **Add OpenClaw** and optionally add it to a channel. Connection options contains the display name. A missing approved Gateway shows a server-setup message.

Connecting reads the dedicated agent's policy and model without inference. Every model step checks that policy again and refuses to run if verification fails or deny-all has been removed. The worker sends the pinned backend model in `x-openclaw-model`, with no client tools and a new explicit session key for each step. Gateway changes are controlled by its trusted administrator; policy verification cannot prevent an administrator changing configuration concurrently with a request. Do not point this bridge at a Gateway managed by an untrusted party.

The connection does not mirror existing OpenClaw chats. The app checkpoint supplies its channel history and scoped memory. Cancelling an HTTP request stops local consumption; an inference already accepted by the Gateway may finish and incur usage, but cannot run native tools under the required policy. The app does not retry uncertain inference requests automatically.

References: [OpenClaw Chat Completions](https://docs.openclaw.ai/gateway/openai-http-api), [Tools invoke and operator authority](https://docs.openclaw.ai/gateway/tools-invoke-http-api), [Gateway tool](https://docs.openclaw.ai/tools/gateway), [Device-paired client alternative](https://docs.openclaw.ai/gateway/clients).

## Brand artwork

The Codex and Cursor vector marks and OpenClaw icon use artwork from [Lobe Icons](https://github.com/lobehub/lobe-icons), copyright 2023 LobeHub, under the MIT license included at `frontend/src/components/icons/LICENSE.lobe-icons` and distributed at `/LICENSE.lobe-icons.txt`. Claude's existing mark is provided by the assistant-ui element catalog.
