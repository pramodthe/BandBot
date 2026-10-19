import { ArrowUpRightIcon, ChevronRightIcon, LoaderCircleIcon, MessageCircleIcon, PlusIcon } from "lucide-react";
import type { State } from "@/store";
import type { Agent } from "@/types";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import type { Modal } from "@/components/workspace/types";
import AgentPresence, { getAgentActivity } from "./AgentPresence";
import type { SelectedMember } from "./MemberDirectory";

export default function TeamOverview({ state, onSelect, onMessage, opening, error, openModal, onChannel }: {
  state: State;
  onSelect: (member: SelectedMember) => void;
  onMessage: (agent: Agent) => void;
  opening: number | null;
  error: string;
  openModal: (modal: Modal) => void;
  onChannel: (id: number) => void;
}) {
  const agents = Object.values(state.agents).sort((a, b) => Number(b.is_master) - Number(a.is_master) || a.name.localeCompare(b.name));
  const people = Object.values(state.users).sort((a, b) => Number(b.online) - Number(a.online) || a.display_name.localeCompare(b.display_name));
  const channels = Object.values(state.channels).filter((channel) => !channel.is_dm).sort((a, b) => a.name.localeCompare(b.name));

  return (
    <section aria-label="Team overview" className="min-h-0 flex-1 overflow-y-auto px-6 py-10 min-[1100px]:px-12 min-[1500px]:py-16">
      <div className="mx-auto w-full max-w-[1000px]">
        <header className="mb-9 flex flex-wrap items-start justify-between gap-5">
          <div>
            <p className="text-muted-foreground mb-2 text-xs font-medium tracking-widest uppercase">People & AI, together</p>
            <h1 className="text-3xl font-semibold tracking-tight">Your team.</h1>
            <p className="text-muted-foreground mt-2 max-w-lg text-sm leading-relaxed">Find the right teammate. Start a conversation. Get something done.</p>
          </div>
          <Button variant="outline" className="rounded-xl" onClick={() => openModal({ kind: "new-agent" })}><PlusIcon />New teammate</Button>
        </header>

        <div className="mb-4 flex items-center justify-between gap-3">
          <h2 className="text-sm font-medium">AI teammates <span className="text-muted-foreground ml-1 font-normal">{agents.length}</span></h2>
          <span className="text-muted-foreground text-xs">Profiles, skills & computers</span>
        </div>
        <div className="grid gap-4 min-[1100px]:grid-cols-2">
          {agents.map((agent) => {
            const activity = getAgentActivity(state, agent.id);
            const leader = agent.is_master;
            const roleDescription = state.roles.find((role) => role.id === agent.role)?.description;
            const description = agent.is_master
              ? agent.specialty || "Bring a task to Master to coordinate work across your team."
              : roleDescription && roleDescription !== agent.name && roleDescription !== agent.role_label && roleDescription !== agent.specialty ? roleDescription : null;
            return (
              <article key={agent.id} aria-label={`${agent.name} teammate`} className={`border-border bg-muted/30 rounded-2xl border p-5 transition-colors hover:bg-muted/65 ${leader ? "min-[1100px]:col-span-2 min-[1100px]:p-7" : ""}`}>
                <div className={`flex items-start ${leader ? "gap-5 min-[1100px]:gap-7" : "gap-4"}`}>
                  <button aria-label={`Open ${agent.name} profile`} onClick={() => onSelect({ kind: "agent", id: agent.id })} className="shrink-0 rounded-2xl outline-none focus-visible:ring-2 focus-visible:ring-ring">
                    <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} size={leader ? 96 : 64} working={activity?.status === "running"} />
                  </button>
                  <div className="min-w-0 flex-1">
                    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2">
                      <button className={`truncate rounded text-left font-semibold outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring ${leader ? "text-xl" : "text-base"}`} onClick={() => onSelect({ kind: "agent", id: agent.id })}>{agent.name}</button>
                      <AgentPresence agent={agent} activity={activity} />
                    </div>
                    <p className="text-muted-foreground mt-1 text-xs">{leader ? "Head of agents" : agent.specialty || agent.role_label}</p>
                    {description ? <p className="text-muted-foreground mt-3 line-clamp-2 text-sm leading-relaxed">{description}</p> : null}
                    <div className="mt-5 flex flex-wrap items-center gap-2">
                      <Button variant={leader ? "default" : "secondary"} size="sm" className="rounded-lg" aria-label={`Message ${agent.name}`} disabled={opening !== null} onClick={() => onMessage(agent)}>
                        {opening === agent.id ? <LoaderCircleIcon className="animate-spin" /> : <MessageCircleIcon />}{opening === agent.id ? "Opening…" : "Message"}
                      </Button>
                      <Button variant="ghost" size="sm" className="text-muted-foreground rounded-lg" aria-label={`Explore ${agent.name} profile`} onClick={() => onSelect({ kind: "agent", id: agent.id })}>Profile<ArrowUpRightIcon /></Button>
                    </div>
                  </div>
                </div>
                {activity ? <button className="border-border text-muted-foreground mt-4 flex w-full items-center gap-2 border-t pt-3 text-left text-xs outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring" onClick={() => onChannel(activity.channel_id)}><span className="min-w-0 flex-1 truncate">{activity.progress || (activity.status === "waiting" ? "Open the conversation to continue" : "View current task")}</span><ChevronRightIcon className="size-3.5 shrink-0" /></button> : null}
              </article>
            );
          })}
          {agents.length === 0 ? <div className="border-border col-span-full rounded-2xl border border-dashed p-8"><h3 className="font-medium">Build your AI team</h3><p className="text-muted-foreground mt-2 text-sm">Create a teammate with a name, a specialty, and a character of its own.</p><Button className="mt-5" onClick={() => openModal({ kind: "new-agent" })}><PlusIcon />Create your first agent</Button></div> : null}
        </div>
        {error ? <p role="alert" className="text-destructive mt-4 text-sm">{error}</p> : null}

        <div className="border-border mt-10 flex flex-wrap gap-x-12 gap-y-7 border-t pt-7">
          <section className="min-w-0 flex-1 basis-64" aria-label="People on your team">
            <h2 className="mb-4 text-sm font-medium">People <span className="text-muted-foreground ml-1 font-normal">{people.length}</span></h2>
            <div className="flex flex-wrap gap-2">
              {people.map((person) => <button key={person.id} className="hover:bg-muted flex max-w-full items-center gap-2 rounded-xl p-2 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Open ${person.display_name} profile`} onClick={() => onSelect({ kind: "human", id: person.id })}><Avatar name={person.display_name} kind="human" size={28} /><span className="truncate text-sm">{person.display_name}</span>{person.id === state.me.id ? <span className="text-muted-foreground text-xs">you</span> : null}</button>)}
            </div>
          </section>
          {channels.length ? <section className="min-w-0 flex-1 basis-64" aria-label="Shared channels"><h2 className="mb-4 text-sm font-medium">Work together in a channel</h2><div className="flex flex-wrap gap-2">{channels.map((channel) => <Button key={channel.id} variant="ghost" size="sm" className="text-muted-foreground max-w-full rounded-lg" onClick={() => onChannel(channel.id)}><span className="truncate"># {channel.name}</span><ChevronRightIcon className="size-3.5" /></Button>)}</div></section> : null}
        </div>
      </div>
    </section>
  );
}
