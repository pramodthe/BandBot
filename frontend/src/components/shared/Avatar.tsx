import type { CSSProperties } from "react";
import type { AgentRuntime } from "@/types";
import RuntimeIcon from "@/components/agents/RuntimeIcon";
import { Avatar as AvatarRoot, AvatarFallback } from "@/components/ui/avatar";

const PALETTE = ["#3d6c32", "#6c7753", "#8d6852", "#5a7286", "#89729b"];

export const AGENT_AVATARS = ["teal", "sage", "blue", "plum", "terracotta", "violet"] as const;
type AgentAvatar = typeof AGENT_AVATARS[number];
export const AGENT_CHARACTERS: Record<AgentAvatar, { label: string; color: string; shape: string; tilt: number; eyes: number }> = {
  teal: { label: "Teal scout", color: "#46c9cf", shape: "M17 8h30a9 9 0 0 1 9 9v30a9 9 0 0 1-9 9H17a9 9 0 0 1-9-9V17a9 9 0 0 1 9-9Z", tilt: -9, eyes: -12 },
  sage: { label: "Green bean", color: "#a4cf75", shape: "M32 5c14 0 22 13 22 28S46 59 32 59 10 48 10 33 18 5 32 5Z", tilt: 12, eyes: 0 },
  blue: { label: "Blue prism", color: "#79aaff", shape: "M22 6h20L58 23v20L42 58H22L6 42V22Z", tilt: 5, eyes: 10 },
  plum: { label: "Pink pebble", color: "#ee91af", shape: "M33 6c15-2 26 10 25 25-1 17-12 28-28 27C15 57 5 46 6 31 7 17 18 7 33 6Z", tilt: -6, eyes: 0 },
  terracotta: { label: "Orange spark", color: "#ffad64", shape: "m32 3 7 8 11-2 2 11 10 6-5 10 4 10-11 4-4 11-11-3-10 5-6-10-11-1 1-12-7-8 8-8-1-11 12-1Z", tilt: 4, eyes: -8 },
  violet: { label: "Violet kite", color: "#b49af5", shape: "M24 7c5-4 12-4 17 0l16 17c4 5 4 12 0 17L41 57c-5 4-12 4-17 0L7 41c-4-5-4-12 0-17Z", tilt: -8, eyes: 12 },
};
const ROLE_AVATARS: Record<string, AgentAvatar> = {
  master: "teal", coordinator: "teal", coder: "sage", critic: "blue", custom: "plum", writer: "terracotta", researcher: "violet",
};

export function agentAvatar(avatar?: string | null, role?: string): AgentAvatar {
  return AGENT_AVATARS.includes(avatar as AgentAvatar) ? avatar as AgentAvatar : ROLE_AVATARS[role ?? ""] ?? "plum";
}

function colorFor(seed: string): string {
  let hash = 0;
  for (const ch of seed) hash = (hash * 31 + ch.charCodeAt(0)) | 0;
  return PALETTE[Math.abs(hash) % PALETTE.length];
}

function AgentFace({ character }: { character: AgentAvatar }) {
  const { color, shape, tilt, eyes } = AGENT_CHARACTERS[character];
  return (
    <svg viewBox="0 0 64 64" className="size-full overflow-visible" aria-hidden="true">
      <g transform={`rotate(${tilt} 32 32)`}>
        <g className="agent-character">
          <path d={shape} fill={color} />
          <g className="agent-eyes" fill="#172126">
            <rect x="22" y="25" width="6" height="12" rx="3" transform={`rotate(${eyes} 25 31)`} />
            <rect x="37" y="25" width="6" height="12" rx="3" transform={`rotate(${eyes} 40 31)`} />
          </g>
        </g>
      </g>
    </svg>
  );
}

export default function Avatar({
  name,
  kind,
  role,
  avatar,
  size = 36,
  working = false,
  runtime,
}: {
  name: string;
  kind: "human" | "agent" | "system" | "external";
  role?: string;
  avatar?: string | null;
  size?: number;
  working?: boolean;
  runtime?: AgentRuntime;
}) {
  if (kind === "agent" && runtime && runtime !== "deepagents") {
    return <span title={name} className="bg-muted inline-flex shrink-0 items-center justify-center rounded-xl"
      style={{ width: size, height: size }}><span style={{ width: size * 0.65, height: size * 0.65 }}><RuntimeIcon runtime={runtime} className="size-full" /></span></span>;
  }
  if (kind === "agent") {
    const character = agentAvatar(avatar, role);
    const delay = Array.from(name).reduce((sum, ch) => sum + ch.charCodeAt(0), 0) % 7;
    return (
      <span className="agent-avatar inline-flex shrink-0 items-center justify-center" title={name}
        data-character={character} data-working={working || undefined}
        style={{ width: size, height: size, "--agent-blink-delay": `-${delay}s` } as CSSProperties}>
        <AgentFace character={character} />
      </span>
    );
  }
  return (
    <AvatarRoot
      style={{ width: size, height: size, fontSize: size * 0.48 }}
      title={kind === "system" ? undefined : name}
    >
      <AvatarFallback
        className="text-[length:inherit] leading-none font-normal text-white"
        style={{ background: kind === "system" ? "#8a8882" : colorFor(name) }}
      >
        {kind === "system" ? "!" : name.trim().charAt(0).toLowerCase()}
      </AvatarFallback>
    </AvatarRoot>
  );
}
