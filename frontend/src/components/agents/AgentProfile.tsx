import { useState } from "react";
import { ArrowLeftIcon, BrainIcon, CalendarDaysIcon, ChevronDownIcon, ChevronRightIcon, HashIcon, LoaderCircleIcon, MessageCircleIcon, MoreHorizontalIcon, PencilIcon, RotateCwIcon, Trash2Icon, UserRoundIcon, XIcon } from "lucide-react";
import { api } from "@/api";
import { isAdmin, type State } from "@/store";
import type { Agent } from "@/types";
import Avatar from "@/components/shared/Avatar";
import AgentComputers from "@/components/computer/AgentComputers";
import AgentInstructions from "./AgentInstructions";
import AgentPlugins from "./AgentPlugins";
import AgentSkills from "./AgentSkills";
import AgentPresence, { getAgentActivity } from "./AgentPresence";
import RuntimeIcon from "./RuntimeIcon";
import { runtimeLabels } from "./runtime-options";
import ProfileSection, { ProfileRow } from "@/components/shared/ProfileSection";
import { Button } from "@/components/ui/button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuGroup, DropdownMenuItem, DropdownMenuSeparator, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { Modal } from "@/components/workspace/types";
import { cn } from "@/lib/utils";

export default function AgentProfile({ state, agent, openModal, onMessage, opening, error, onBack, onChannel, onUpdated, onDeleted, pluginsRevision, embedded = false, onClose }: {
  state: State;
  agent: Agent;
  openModal: (modal: Modal) => void;
  onMessage: (agent: Agent) => void;
  opening: number | null;
  error: string;
  onBack: () => void;
  onChannel: (id: number) => void;
  onUpdated: (agent: Agent) => void;
  onDeleted: (agentId: number) => void;
  pluginsRevision: number;
  embedded?: boolean;
  onClose?: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const [tab, setTab] = useState("profile");
  const [pluginsOpened, setPluginsOpened] = useState(false);
  const canManage = agent.owner_id == null || agent.owner_id === state.me.id;
  const role = state.roles.find((item) => item.id === agent.role);
  const description = role?.description && role.description !== agent.name && role.description !== agent.role_label ? role.description : agent.is_master ? "Coordinates work across your team. Start a conversation or bring Master into a shared channel." : agent.specialty || agent.role_label;
  const creator = agent.created_by_agent ? state.agents[agent.created_by_agent]?.name : agent.created_by ? state.users[agent.created_by]?.display_name : null;
  const channels = Object.values(state.channels).filter((channel) => !channel.is_dm && channel.agent_ids.includes(agent.id));
  const activity = getAgentActivity(state, agent.id);
  const edit = () => openModal({ kind: "agent-settings", agentId: agent.id });
  const run = async (operation: () => Promise<void>, message: string) => {
    if (busy) return;
    setBusy(true); setActionError(""); setNotice("");
    try { await operation(); setNotice(message); }
    catch (cause) { setActionError((cause as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <section aria-label={`${agent.name} profile`} className={cn("min-h-0 flex-1 overflow-y-auto px-6 pb-8", embedded ? "pt-3" : "py-6 sm:px-8 sm:py-10")}>
      <div className={cn("mx-auto flex w-full max-w-[672px] flex-col", embedded && "max-w-none")}>
        <div className="bg-background sticky top-0 z-10 mb-4 flex min-h-9 items-center justify-between">
          {!embedded ? <Button variant="ghost" size="sm" onClick={onBack}><ArrowLeftIcon data-icon="inline-start" />Team</Button> : <span className="text-muted-foreground text-xs">Agent profile</span>}
          <div className="flex items-center gap-1">
            <DropdownMenu>
              <DropdownMenuTrigger asChild><Button variant="ghost" size="icon-sm" aria-label={`More actions for ${agent.name}`} disabled={busy || !canManage}>{busy ? <LoaderCircleIcon className="animate-spin" /> : <MoreHorizontalIcon />}</Button></DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuGroup>
                  <DropdownMenuItem onSelect={edit}><PencilIcon />Edit profile</DropdownMenuItem>
                  <DropdownMenuItem onSelect={() => { void run(async () => onUpdated(await api<Agent>("POST", `/api/agents/${agent.id}/restart`)), "Agent restarted."); }}><RotateCwIcon />Restart agent</DropdownMenuItem>
                </DropdownMenuGroup>
                {!agent.is_master ? <><DropdownMenuSeparator /><DropdownMenuGroup>
                  <DropdownMenuItem variant="destructive" disabled={!canManage || (agent.owner_id == null && !isAdmin(state))} onSelect={() => {
                    if (confirm(agent.band_auth === "platform" ? `Delete ${agent.name} from this app? Your Band connection and Band history will be kept.`
                      : agent.band_auth === "managed" ? `Delete ${agent.name} from this app and your Band account? Conversation history will be kept.`
                      : `Disconnect ${agent.name} from this app? The original agent and its history on Band will be kept.`)) {
                      void run(async () => { await api("DELETE", `/api/agents/${agent.id}`); onDeleted(agent.id); }, "Agent deleted.");
                    }
                  }}><Trash2Icon />{agent.band_auth === "agent_key" ? "Disconnect agent" : "Delete agent"}</DropdownMenuItem>
                </DropdownMenuGroup></> : null}
              </DropdownMenuContent>
            </DropdownMenu>
            {onClose ? <Button variant="ghost" size="icon-sm" aria-label="Close agent profile" onClick={onClose}><XIcon /></Button> : null}
          </div>
        </div>
        <header className="mb-6 flex flex-col items-center text-center">
          <button type="button" className="shrink-0 rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring" aria-label={`Edit ${agent.name} avatar`} disabled={!canManage} onClick={edit}>
            <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} runtime={agent.runtime} size={embedded ? 96 : 112} working={activity?.status === "running"} />
          </button>
          <h1 className="mt-4 max-w-full text-2xl font-semibold tracking-tight break-words">{agent.name}</h1>
          <p className="text-muted-foreground mt-1 text-sm break-words">{agent.specialty || (agent.is_master ? "Team coordination" : agent.role_label)}</p>
          <div className="mt-3"><AgentPresence agent={agent} activity={activity} /></div>
          <Button variant="outline" className="mt-5 h-10 w-full max-w-sm rounded-xl" aria-label={`Message ${agent.name}`} disabled={opening === agent.id || !canManage} onClick={() => onMessage(agent)}>
            {opening === agent.id ? <LoaderCircleIcon className="animate-spin" /> : <MessageCircleIcon />}<span className="truncate">{opening === agent.id ? "Opening conversation…" : `Message ${agent.name}`}</span>
          </Button>
        </header>
        {error || actionError ? <p role="alert" className="text-destructive text-sm">{error || actionError}</p> : null}
        {notice ? <p role="status" className="text-muted-foreground text-sm">{notice}</p> : null}
        <Tabs value={tab} onValueChange={(value) => { setTab(value); if (value === "plugins") setPluginsOpened(true); }} className="gap-4">
          <div className="overflow-x-auto overflow-y-hidden"><TabsList className="h-10 w-full rounded-xl" aria-label="Agent profile sections">
            <TabsTrigger value="profile">Overview</TabsTrigger>
            <TabsTrigger value="skills">Skills</TabsTrigger>
            <TabsTrigger value="plugins">Apps</TabsTrigger>
            <TabsTrigger value="computer">Computer</TabsTrigger>
          </TabsList></div>
          <TabsContent forceMount value="profile" className="flex flex-col data-[state=inactive]:hidden">
            <ProfileSection title="About" action={<Button variant="ghost" size="icon-sm" aria-label={`Edit ${agent.name} profile`} disabled={!canManage} onClick={edit}><PencilIcon /></Button>}>
              <p className="text-muted-foreground text-sm leading-relaxed">{description}</p>
            </ProfileSection>
            <AgentInstructions agent={agent} roleInstructions={role?.instructions} canManage={canManage} onUpdated={onUpdated} />
            <ProfileSection title="Channels">
              {channels.length ? <div className="flex flex-col gap-1">{channels.map((channel) => <Button key={channel.id} variant="ghost" className="w-full min-w-0 justify-start px-0 text-sm font-normal hover:px-2" onClick={() => onChannel(channel.id)}><HashIcon className="text-muted-foreground" /><span className="truncate">{channel.name}</span><ChevronRightIcon className="text-muted-foreground ml-auto" /></Button>)}</div> : <p className="text-muted-foreground text-sm">Not in a shared channel yet. Start a private conversation to work together.</p>}
            </ProfileSection>
            <details className="group border-border border-t py-5">
              <summary className="flex min-h-8 cursor-pointer list-none items-center justify-between text-sm font-semibold [&::-webkit-details-marker]:hidden">Agent details<ChevronDownIcon className="text-muted-foreground size-4 transition-transform group-open:rotate-180" /></summary>
              <dl className="mt-3 text-sm">
                <ProfileRow label="Runtime" icon={<RuntimeIcon runtime={agent.runtime ?? "deepagents"} />}>{runtimeLabels[agent.runtime ?? "deepagents"]}</ProfileRow>
                <ProfileRow label="Model" icon={<BrainIcon />}>{state.models.find((model) => model.id === agent.model)?.label ?? agent.model}</ProfileRow>
                {creator ? <ProfileRow label="Created by" icon={<UserRoundIcon />}>{creator}</ProfileRow> : null}
                {agent.created_at ? <ProfileRow label="Created at" icon={<CalendarDaysIcon />}>{new Date(agent.created_at).toLocaleDateString(undefined, { day: "numeric", month: "long", year: "numeric" })}</ProfileRow> : null}
              </dl>
            </details>
          </TabsContent>
          <TabsContent value="skills"><AgentSkills agentId={agent.id} canManage={canManage && isAdmin(state)} revision={state.skillsRevision} /></TabsContent>
          <TabsContent forceMount value="plugins" className="data-[state=inactive]:hidden">{pluginsOpened && canManage && isAdmin(state) ? <AgentPlugins agent={agent} revision={pluginsRevision} onUpdated={onUpdated} onConnections={() => openModal({ kind: "plugins" })} /> : null}</TabsContent>
          <TabsContent value="computer"><AgentComputers agent={agent} preferredChannelId={state.activeChannelId} onChannel={onChannel} onMessage={() => onMessage(agent)} /></TabsContent>
        </Tabs>
      </div>
    </section>
  );
}
