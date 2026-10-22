import { useState } from "react";
import { ChevronRightIcon } from "lucide-react";
import type { State } from "@/store";
import type { AgentRuntime, Channel } from "@/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import CreateAgentModal from "./CreateAgentModal";
import ConnectAgentModal from "./ConnectAgentModal";
import RuntimeIcon from "./RuntimeIcon";

const options: { runtime: AgentRuntime; title: string; description: string }[] = [
  { runtime: "deepagents", title: "Create agent", description: "Give a teammate a name, specialty, and instructions." },
  { runtime: "codex", title: "Codex", description: "Connect an account and work with Codex in your channels." },
  { runtime: "claude", title: "Claude Agent", description: "Connect Anthropic and add a Claude-powered teammate." },
  { runtime: "cursor", title: "Cursor", description: "Connect Cursor and work with its agent in your channels." },
  { runtime: "openclaw", title: "OpenClaw", description: "Connect a Gateway and add an OpenClaw teammate." },
];

export default function AddAgentModal({ state, channel, onClose }: {
  state: State; channel: Channel | null; onClose: () => void;
}) {
  const [runtime, setRuntime] = useState<AgentRuntime | null>(null);
  if (runtime === "deepagents") return <CreateAgentModal state={state} channel={channel} onClose={onClose} onBack={() => setRuntime(null)} />;
  if (runtime) return <ConnectAgentModal state={state} channel={channel} runtime={runtime} onClose={onClose} onBack={() => setRuntime(null)} />;
  return <Dialog open onOpenChange={(open) => !open && onClose()}>
    <DialogContent className="max-h-[calc(100dvh-32px)] overflow-y-auto rounded-2xl p-6 sm:max-w-[480px]">
      <DialogHeader className="text-left"><DialogTitle>Add agents</DialogTitle>
        <DialogDescription>Choose a teammate to work alongside your team.</DialogDescription></DialogHeader>
      <div className="mt-2 space-y-3">{options.map((option) => <Button key={option.runtime} variant="outline"
        className="h-auto w-full justify-start gap-4 rounded-xl p-4 text-left whitespace-normal"
        onClick={() => setRuntime(option.runtime)}>
        <span className="bg-muted flex size-11 shrink-0 items-center justify-center rounded-xl"><RuntimeIcon runtime={option.runtime} className="size-6" /></span>
        <span className="min-w-0 flex-1"><span className="block text-sm font-semibold">{option.title}</span>
          <span className="text-muted-foreground mt-1 block text-xs font-normal leading-relaxed">{option.description}</span></span>
        <ChevronRightIcon className="text-muted-foreground size-4 shrink-0" />
      </Button>)}</div>
    </DialogContent>
  </Dialog>;
}
