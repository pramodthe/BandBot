export type Modal =
  | { kind: "new-agent" }
  | { kind: "new-channel" }
  | { kind: "people"; channelId?: number; personId?: number }
  | { kind: "agent"; agentId: number }
  | { kind: "agent-settings"; agentId: number }
  | { kind: "plugins" }
  | { kind: "openai" }
  | { kind: "elements" }
  | null;

export type WorkspaceView = "chat" | "members";
export type OpenComputer = (agentId: number, taskId: string) => void;
