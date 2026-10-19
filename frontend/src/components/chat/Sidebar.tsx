import { useEffect, useRef, useState } from "react";
import { HashIcon, PlusIcon, UserRoundIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { State } from "@/store";
import type { Agent } from "@/types";
import Avatar from "@/components/shared/Avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import type { Modal } from "@/components/workspace/types";
import { getAgentActivity } from "@/components/agents/AgentPresence";

function UnreadBadge({ count }: { count: number }) {
  return count > 0 ? <Badge className="ml-auto rounded-full px-1.5 text-[10px]">{count}</Badge> : null;
}

export default function Sidebar({ state, onChannel, onMessage, openModal, searchRevision, opening, error, listOpen }: {
  state: State;
  onChannel: (id: number) => void;
  onMessage: (agent: Agent) => void;
  openModal: (modal: Modal) => void;
  searchRevision: number;
  opening: number | null;
  error: string;
  listOpen: boolean;
}) {
  const [query, setQuery] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  useEffect(() => { if (searchRevision > 0) searchInput.current?.focus(); }, [searchRevision]);
  const search = query.trim().toLowerCase();
  const channels = Object.values(state.channels).filter((channel) => !channel.is_dm && channel.name.toLowerCase().includes(search)).sort((a, b) => a.name.localeCompare(b.name));
  const agents = Object.values(state.agents).filter((agent) => agent.name.toLowerCase().includes(search)).sort((a, b) => Number(b.is_master) - Number(a.is_master) || a.name.localeCompare(b.name));
  const dms = Object.values(state.channels).filter((channel) => channel.is_dm);

  return (
    <aside aria-label="Conversations" className={cn("border-border bg-sidebar flex w-[280px] shrink-0 flex-col border-r narrow:w-[248px] max-sm:min-w-0 max-sm:flex-1", !listOpen && "max-sm:hidden")}>
      <header className="flex h-16 shrink-0 items-center justify-between px-5">
        <h1 className="text-base font-semibold tracking-tight">Conversations</h1>
        <Button variant="ghost" size="icon-sm" aria-label="Create channel" onClick={() => openModal({ kind: "new-channel" })}><PlusIcon /></Button>
      </header>
      <div className="px-4 pb-3"><Input ref={searchInput} aria-label="Find conversations by name" placeholder="Find conversations" className="h-10 rounded-xl bg-muted/50" value={query} onChange={(event) => setQuery(event.target.value)} /></div>
      <div className="min-h-0 flex-1 overflow-y-auto pb-4">
        <div className="text-muted-foreground flex h-10 items-center justify-between px-5 text-xs"><h2>Channels</h2><Button variant="ghost" size="icon-xs" aria-label="New channel in list" onClick={() => openModal({ kind: "new-channel" })}><PlusIcon /></Button></div>
        {channels.map((channel) => (
          <button key={channel.id} aria-current={state.activeChannelId === channel.id ? "page" : undefined} className={cn("hover:bg-muted mx-2 flex min-h-11 w-[calc(100%-1rem)] items-center gap-3 rounded-xl px-3 py-2.5 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring", state.activeChannelId === channel.id && "bg-accent")} onClick={() => onChannel(channel.id)}>
            <HashIcon className="text-muted-foreground size-[18px]" strokeWidth={1.5} /><span className="truncate text-sm font-medium">{channel.name}</span><UnreadBadge count={state.unread[channel.id] ?? 0} />
          </button>
        ))}
        {channels.length === 0 ? <p className="text-muted-foreground px-5 py-3 text-sm">No channels found.</p> : null}
        <div className="text-muted-foreground mt-5 flex h-10 items-center justify-between px-5 text-xs"><h2>AI teammates</h2><Button variant="ghost" size="sm" className="h-8 px-1.5 text-xs" aria-label="Create agent" onClick={() => openModal({ kind: "new-agent" })}><PlusIcon className="size-3.5" />New agent</Button></div>
        {agents.map((agent) => {
          const dm = dms.find((channel) => channel.dm_agent_id === agent.id);
          const activity = getAgentActivity(state, agent.id);
          const lastMessage = dm ? state.messages[dm.id]?.filter((message) => message.to_humans).at(-1) : undefined;
          return (
            <div key={agent.id} className={cn("group relative mx-2 my-1 flex items-center rounded-xl hover:bg-muted", dm?.id === state.activeChannelId && "bg-accent")}>
              <button className="flex min-h-16 min-w-0 flex-1 items-center gap-3 rounded-xl py-3 pr-10 pl-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring" disabled={opening === agent.id} onClick={() => onMessage(agent)} aria-label={`Chat with ${agent.name}`}>
                <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} size={40} working={activity?.status === "running"} />
                <span className="flex min-w-0 flex-1 flex-col gap-0.5"><span className="truncate text-sm font-medium">{agent.name}</span><span className="text-muted-foreground truncate text-xs">{opening === agent.id ? "Opening conversation…" : activity?.status === "waiting" ? "Needs attention" : activity?.status === "running" ? activity.progress || "Working…" : lastMessage?.content.replace(/[#*`]/g, "") || agent.specialty || (agent.is_master ? "Team coordination" : agent.role_label)}</span></span>
                <UnreadBadge count={dm ? state.unread[dm.id] ?? 0 : 0} />
              </button>
              <Button variant="ghost" size="icon-sm" className="absolute right-1 opacity-0 group-hover:opacity-100 focus-visible:opacity-100 max-sm:opacity-100" aria-label={`View ${agent.name} profile`} onClick={() => openModal({ kind: "agent", agentId: agent.id })}><UserRoundIcon /></Button>
            </div>
          );
        })}
        {agents.length === 0 ? <p className="text-muted-foreground px-5 py-3 text-sm">No agents found.</p> : null}
        {error ? <p className="text-destructive px-5 py-3 text-sm" role="alert">{error}</p> : null}
      </div>
    </aside>
  );
}
