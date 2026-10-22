import { ArrowLeftIcon, CircleDotIcon, HashIcon, UserRoundIcon } from "lucide-react";
import { roleOf, type State } from "@/store";
import type { Agent } from "@/types";
import Avatar from "@/components/shared/Avatar";
import AgentProfile from "./AgentProfile";
import type { SelectedMember } from "./MemberDirectory";
import ProfileSection, { ProfileRow } from "@/components/shared/ProfileSection";
import StatusDot from "@/components/shared/StatusDot";
import TeamOverview from "./TeamOverview";
import { Button } from "@/components/ui/button";
import type { Modal } from "@/components/workspace/types";

export default function MemberProfile({ state, selected, onSelect, openModal, onMessage, opening, error, onBack, onChannel, onUpdated, onDeleted, pluginsRevision }: {
  state: State;
  selected: SelectedMember;
  onSelect: (member: SelectedMember) => void;
  openModal: (modal: Modal) => void;
  onMessage: (agent: Agent) => void;
  opening: number | null;
  error: string;
  onBack: () => void;
  onChannel: (id: number) => void;
  onUpdated: (agent: Agent) => void;
  onDeleted: (agentId: number) => void;
  pluginsRevision: number;
}) {
  const agent = selected?.kind === "agent" ? state.agents[selected.id] : null;
  const human = selected?.kind === "human" ? state.users[selected.id] : null;
  if (agent) return <AgentProfile key={agent.id} {...{ state, agent, openModal, onMessage, opening, error, onBack, onChannel, onUpdated, onDeleted, pluginsRevision }} />;
  if (!human) return <TeamOverview {...{ state, onSelect, onMessage, opening, error, openModal, onChannel }} />;
  const channels = Object.values(state.channels).filter((channel) => !channel.is_dm && channel.human_ids.includes(human.id));

  return (
    <section aria-label={`${human.display_name} profile`} className="min-h-0 flex-1 overflow-y-auto px-5 py-5 min-[640px]:px-6 min-[640px]:py-10 min-[901px]:px-8 min-[901px]:py-16">
      <div className="mx-auto flex w-full max-w-[752px] flex-col gap-5">
        <Button variant="ghost" size="sm" className="hidden self-start max-sm:inline-flex" onClick={onBack}><ArrowLeftIcon data-icon="inline-start" />Members</Button>
        <header className="mb-1 flex flex-wrap items-center gap-3.5"><Avatar name={human.display_name} kind="human" size={64} /><h1 className="min-w-0 flex-1 truncate text-base font-semibold">{human.display_name}</h1>{human.id !== state.me.id ? <Button variant="outline" onClick={() => openModal({ kind: "people", personId: human.id })}>Connect or message</Button> : <Button variant="outline" onClick={() => openModal({ kind: "people" })}>People</Button>}</header>
        <ProfileSection title="PROFILE">
          <dl className="text-sm">
            <ProfileRow label="Role" icon={<UserRoundIcon />}><span className="capitalize">{roleOf(state, human.id)}</span></ProfileRow>
            <ProfileRow label="Username" icon={<UserRoundIcon />}>@{human.username}</ProfileRow>
            <ProfileRow label="Status" icon={<CircleDotIcon />}><span className="inline-flex items-center gap-2"><StatusDot state={human.online ? "online" : "stopped"} />{human.online ? "Online" : "Offline"}</span></ProfileRow>
          </dl>
        </ProfileSection>
        {channels.length ? <ProfileSection title="CHANNELS"><div className="flex flex-wrap gap-2">{channels.map((channel) => <Button key={channel.id} variant="ghost" size="sm" className="max-w-full" onClick={() => onChannel(channel.id)}><HashIcon data-icon="inline-start" /><span className="truncate">{channel.name}</span></Button>)}</div></ProfileSection> : null}
      </div>
    </section>
  );
}
