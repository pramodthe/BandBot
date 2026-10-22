import { BotIcon } from "lucide-react";
import { ClaudeLogo } from "@/components/assistant-ui/elements/logos";
import { CodexIcon } from "@/components/icons/codex";
import { CursorIcon } from "@/components/icons/cursor";
import type { AgentRuntime } from "@/types";

export default function RuntimeIcon({ runtime, className = "size-5" }: { runtime: AgentRuntime; className?: string }) {
  if (runtime === "openclaw") return <img src="/openclaw-color.svg" alt="" aria-hidden="true" className={className} />;
  const Icon = runtime === "codex" ? CodexIcon : runtime === "claude" ? ClaudeLogo : runtime === "cursor" ? CursorIcon : BotIcon;
  return <Icon className={className} aria-hidden="true" />;
}
