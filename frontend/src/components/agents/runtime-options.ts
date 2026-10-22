import type { AgentRuntime, ConnectedAgentRuntime } from "@/types";

export const runtimeLabels: Record<AgentRuntime, string> = {
  deepagents: "Workspace agent", codex: "Codex", claude: "Claude Agent", cursor: "Cursor", openclaw: "OpenClaw",
};

export const connectionLabels: Record<ConnectedAgentRuntime, string> = {
  codex: "OpenAI", claude: "Anthropic", cursor: "Cursor", openclaw: "OpenClaw Gateway",
};
