import type { AgentConnection, AgentConnectionsResponse } from "@/types";

function record(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function connection(value: unknown): value is AgentConnection {
  return record(value) && typeof value.id === "string" && typeof value.label === "string"
    && ["codex", "claude", "cursor", "openclaw"].includes(String(value.provider))
    && ["api_key", "chatgpt", "codex_chatgpt", "gateway_token"].includes(String(value.auth_method))
    && Array.isArray(value.models) && value.models.every((model) => typeof model === "string");
}

export function connectionResponse(value: unknown): AgentConnectionsResponse {
  if (!record(value) || !Array.isArray(value.connections) || !value.connections.every(connection)
    || !record(value.runtimes) || typeof value.runtimes.codex !== "boolean" || typeof value.runtimes.claude !== "boolean"
    || typeof value.codex_chatgpt_sign_in !== "boolean") {
    throw new Error("Agent connections are unavailable. Restart the API and worker to load the latest version, then retry.");
  }
  return {
    connections: value.connections,
    codex_chatgpt_sign_in: value.codex_chatgpt_sign_in,
    codex_local_sign_in: value.codex_local_sign_in === true,
    runtimes: { codex: value.runtimes.codex, claude: value.runtimes.claude,
      cursor: value.runtimes.cursor === true, openclaw: value.runtimes.openclaw === true },
    openclaw_gateways: Array.isArray(value.openclaw_gateways)
      ? value.openclaw_gateways.filter((url): url is string => typeof url === "string") : [],
  };
}
