import { useEffect, useRef, useState } from "react";
import { PlusIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import { roleOf, type State } from "@/store";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import type { Modal } from "@/components/workspace/types";
import { getAgentActivity } from "./AgentPresence";

export type SelectedMember = { kind: "human" | "agent"; id: number } | null;

export default function MemberDirectory({ state, selected, onSelect, openModal, searchRevision }: {
  state: State;
  selected: SelectedMember;
  onSelect: (member: SelectedMember) => void;
  openModal: (modal: Modal) => void;
  searchRevision: number;
}) {
  const [filter, setFilter] = useState("all");
  const [query, setQuery] = useState("");
  const searchInput = useRef<HTMLInputElement>(null);
  useEffect(() => { if (searchRevision > 0) searchInput.current?.focus(); }, [searchRevision]);
  const search = query.trim().toLowerCase();
  const humans = Object.values(state.users).sort((a, b) =>
    Number(b.id === state.me.id) - Number(a.id === state.me.id) || a.display_name.localeCompare(b.display_name),
  );
  const agents = Object.values(state.agents).sort((a, b) => Number(b.is_master) - Number(a.is_master) || a.name.localeCompare(b.name));
  const members = [
    ...humans.map((user) => ({ id: user.id, kind: "human" as const, name: user.display_name, role: roleOf(state, user.id), avatar: undefined, runtime: undefined, sub: roleOf(state, user.id) })),
    ...agents.map((agent) => ({ id: agent.id, kind: "agent" as const, name: agent.name, role: agent.role, avatar: agent.avatar, runtime: agent.runtime, sub: agent.is_master ? "Head of Agents" : agent.role_label })),
  ].filter((member) => (filter === "all" || member.kind === filter) && member.name.toLowerCase().includes(search));

  return (
    <aside aria-label="Member directory" className={cn("border-border bg-sidebar flex w-[280px] shrink-0 flex-col border-r narrow:w-[248px] max-sm:min-w-0 max-sm:flex-1", selected && "max-sm:hidden")}>
      <header className="flex h-16 shrink-0 items-center justify-between px-5">
        <h1 className="text-base font-semibold">Members</h1>
        <Button variant="ghost" size="icon-sm" aria-label="Add agents" onClick={() => openModal({ kind: "new-agent" })}><PlusIcon /></Button>
      </header>
      <div className="px-5 pb-4">
        <ToggleGroup type="single" variant="filter" size="sm" spacing={1} value={filter} onValueChange={(value) => value && setFilter(value)} aria-label="Filter members">
          <ToggleGroupItem value="all" aria-label="All members">All</ToggleGroupItem>
          <ToggleGroupItem value="human" aria-label="User members">People</ToggleGroupItem>
          <ToggleGroupItem value="agent" aria-label="Agent members">Agents</ToggleGroupItem>
        </ToggleGroup>
      </div>
      <div className="px-4 pb-3"><Input ref={searchInput} aria-label="Find a member" placeholder="Find a member" className="h-10 rounded-xl bg-muted/50" value={query} onChange={(event) => setQuery(event.target.value)} /></div>
      <div className="min-h-0 flex-1 overflow-y-auto">
        {members.map((member) => {
          const activity = member.kind === "agent" ? getAgentActivity(state, member.id) : undefined;
          return (
            <button
              key={`${member.kind}-${member.id}`}
              className={cn("hover:bg-muted focus-visible:ring-ring mx-2 my-1 flex min-h-16 w-[calc(100%-1rem)] items-center gap-3 rounded-xl px-3 py-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset", selected?.kind === member.kind && selected.id === member.id && "bg-accent")}
              aria-label={`View ${member.name} profile`}
              aria-current={selected?.kind === member.kind && selected.id === member.id ? "true" : undefined}
              onClick={() => onSelect({ kind: member.kind, id: member.id })}
            >
              <Avatar name={member.name} kind={member.kind} role={member.role} avatar={member.avatar} runtime={member.runtime} size={40} working={activity?.status === "running"} />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate text-sm font-medium">{member.name}</span>
                <span className={cn("text-muted-foreground truncate text-sm", member.kind === "human" && "capitalize")}>{activity?.status === "waiting" ? "Needs attention" : activity?.status === "running" ? "Working…" : activity?.status === "queued" ? "Queued" : member.sub}</span>
              </span>
            </button>
          );
        })}
        {members.length === 0 ? <p className="text-muted-foreground px-5 py-6 text-sm">No members found.</p> : null}
      </div>
    </aside>
  );
}
