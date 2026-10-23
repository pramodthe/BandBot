import { lazy, Suspense, useEffect, useState, type Dispatch } from "react";
import { api } from "@/api";
import { isAdmin, policyBlock, type Action, type State } from "@/store";
import type { Agent, Channel, ChannelComputer } from "@/types";
import { cn } from "@/lib/utils";
import AgentModal from "@/components/agents/AgentModal";
import AgentProfilePanel from "@/components/agents/AgentProfilePanel";
import ChannelView from "@/components/chat/ChannelView";
import ComputerPanel from "@/components/computer/ComputerPanel";
import AddAgentModal from "@/components/agents/AddAgentModal";
import CreateChannelModal from "@/components/chat/CreateChannelModal";
import MembersPanel from "@/components/chat/MembersPanel";
import MemberDirectory, { type SelectedMember } from "@/components/agents/MemberDirectory";
import MemberProfile from "@/components/agents/MemberProfile";
import PluginsModal from "@/components/settings/PluginsModal";
import Sidebar from "@/components/chat/Sidebar";
import WorkspaceEmpty from "@/components/shared/WorkspaceEmpty";
import WorkspaceNavigation from "./WorkspaceNavigation";
import ProvidersModal from "@/components/settings/ProvidersModal";
import PeopleModal from "@/components/people/PeopleModal";
import type { Modal, OpenComputer, WorkspaceView } from "./types";

const ElementsPanel = lazy(() => import("@/components/generated/ElementsPanel"));

function useChannelComputers(channelId: number | null) {
  const [computers, setComputers] = useState<ChannelComputer[]>([]);

  useEffect(() => {
    setComputers([]);
    if (channelId == null) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const rows = await api<ChannelComputer[]>("GET", `/api/channels/${channelId}/computers`);
        if (stopped) return;
        setComputers(rows);
      } catch {
        // Polling resumes on the next tick; the computer panel reports its own errors.
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId]);

  return computers;
}

export default function Workspace({
  state,
  dispatch,
  onSignOut,
}: {
  state: State;
  dispatch: Dispatch<Action>;
  onSignOut: () => void;
}) {
  const [modal, setModal] = useState<Modal>(null);
  useEffect(() => {
    const open = () => { if (isAdmin(state)) setModal({ kind: "openai" }); };
    window.addEventListener("open-provider-settings", open);
    return () => window.removeEventListener("open-provider-settings", open);
  }, [state.workspaceRoles, state.me.id]);
  const [showMembers, setShowMembers] = useState(false);
  const [view, setView] = useState<WorkspaceView>("chat");
  const [member, setMember] = useState<SelectedMember>(null);
  const [searchRevision, setSearchRevision] = useState(0);
  const [chatListOpen, setChatListOpen] = useState(true);
  const [opening, setOpening] = useState<number | null>(null);
  const [dmError, setDmError] = useState("");
  const [computer, setComputer] = useState<{ agentId: number; taskId: string } | null>(null);
  const [pluginsRevision, setPluginsRevision] = useState(0);
  const [profileId, setProfileId] = useState<number | null>(null);
  const profileAgent = profileId == null ? null : state.agents[profileId];
  const channel = state.activeChannelId != null ? state.channels[state.activeChannelId] : null;
  // A live deletion must also restore the directory on small screens.
  const selectedMember = member && (member.kind === "agent" ? state.agents[member.id] : state.users[member.id]) ? member : null;
  const agentUpdated = (agent: Agent) => dispatch({ type: "event", event: { type: "agent", agent } });
  const agentDeleted = (agentId: number) => dispatch({ type: "event", event: { type: "agent_deleted", agent_id: agentId } });
  const openModal = (next: Modal) => {
    if (next?.kind === "agent") {
      if (view === "chat") {
        setProfileId(next.agentId);
        setComputer(null);
        setShowMembers(false);
      } else setMember({ kind: "agent", id: next.agentId });
      setDmError("");
      setModal(null);
    } else setModal(next);
  };

  const changeView = (next: WorkspaceView) => {
    setProfileId(null);
    setView(next);
    setDmError("");
    setChatListOpen(true);
    if (next === "members") setMember(null);
  };
  const selectChannel = (channelId: number) => {
    setProfileId(null);
    setComputer(null);
    dispatch({ type: "select_channel", channelId });
    setView("chat");
    setChatListOpen(false);
    setDmError("");
    requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('textarea[aria-label="Message"]')?.focus());
  };
  const openDm = async (agent: Agent) => {
    if (opening !== null) return;
    setDmError("");
    const existing = Object.values(state.channels).find((item) => item.is_dm && item.dm_agent_id === agent.id);
    if (existing) return selectChannel(existing.id);
    setOpening(agent.id);
    try {
      const next = await api<Channel>("POST", `/api/agents/${agent.id}/dm`);
      dispatch({ type: "event", event: { type: "channel", channel: next } });
      selectChannel(next.id);
    } catch (error) {
      setDmError(`Couldn't open a chat with ${agent.name}: ${(error as Error).message}`);
    } finally {
      setOpening(null);
    }
  };

  useEffect(() => {
    setComputer(null);
  }, [channel?.id]);

  const computers = useChannelComputers(channel?.id ?? null);

  const openComputer: OpenComputer = (agentId, taskId) => {
    setProfileId(null);
    setShowMembers(false);
    setComputer({ agentId, taskId });
  };
  const closeComputer = () => {
    setComputer(null);
  };
  const shown = computer && computers.find((c) => c.agent_id === computer.agentId);

  return (
    <div className="bg-background text-foreground flex h-dvh min-h-0 overflow-hidden">
      <WorkspaceNavigation state={state} view={view} detailOpen={view === "chat" ? !chatListOpen : !!selectedMember} onView={changeView} onSearch={() => { setSearchRevision((value) => value + 1); setChatListOpen(true); setMember(null); }} openModal={openModal} onSignOut={onSignOut} />
      {view === "members" ? (
        <MemberDirectory state={state} selected={selectedMember} onSelect={(next) => { setMember(next); setDmError(""); }} openModal={openModal} searchRevision={searchRevision} />
      ) : (
        <Sidebar state={state} onChannel={selectChannel} onMessage={openDm} openModal={openModal} searchRevision={searchRevision} opening={opening} error={dmError} listOpen={chatListOpen} />
      )}
      <main className={cn("flex min-w-0 flex-1 flex-col", (view === "members" ? !selectedMember : chatListOpen) && "max-sm:hidden")}>
        {view === "members" ? (
          <MemberProfile state={state} selected={selectedMember} onSelect={(next) => { setMember(next); setDmError(""); }} openModal={openModal} onMessage={openDm} opening={opening} error={dmError} onBack={() => setMember(null)} onChannel={selectChannel} onUpdated={agentUpdated} onDeleted={agentDeleted} pluginsRevision={pluginsRevision} />
        ) : channel ? (
          <>
          <div className="relative flex min-h-0 flex-1">
            <ChannelView
              dispatch={dispatch}
              key={`channel-${channel.id}`}
              state={state}
              channel={channel}
              showMembers={showMembers}
              onToggleMembers={() => { setShowMembers((v) => !v); setProfileId(null); setComputer(null); }}
              onBack={() => setChatListOpen(true)}
              openModal={openModal}
              computers={computers}
              computerOpen={computer !== null}
              openComputer={openComputer}
            />
            {profileAgent ? (
              <AgentProfilePanel key={`profile-${profileAgent.id}`} state={state} agent={profileAgent} openModal={openModal}
                onMessage={openDm} opening={opening} error={dmError} onBack={() => setProfileId(null)}
                onChannel={selectChannel} onUpdated={agentUpdated} onDeleted={agentDeleted}
                pluginsRevision={pluginsRevision} onClose={() => setProfileId(null)} />
            ) : computer ? (
              <ComputerPanel
                key={`computer-${computer.agentId}-${channel.id}`}
                taskId={computer.taskId}
                channelId={channel.id}
                agent={state.agents[computer.agentId]}
                working={shown?.agent_working ?? false}
                liveBlocked={policyBlock(state, channel, "computer_control")}
                others={computers
                  .filter((c) => c.agent_id !== computer.agentId && state.agents[c.agent_id])
                  .map((c) => ({ agent: state.agents[c.agent_id], working: c.agent_working }))}
                onSwitch={(agentId) => {
                  const next = computers.find((c) => c.agent_id === agentId);
                  if (next) openComputer(next.agent_id, next.task_id);
                }}
                onClose={closeComputer}
              />
            ) : (
              showMembers && !channel.is_dm && <MembersPanel key={`members-${channel.id}`} state={state} channel={channel} openModal={openModal} onClose={() => setShowMembers(false)} />
            )}
          </div>
          </>
        ) : (
          <WorkspaceEmpty title="Your next conversation starts here" description="Pick a channel, or create one for your team." />
        )}
      </main>
      {modal?.kind === "new-channel" && (
        <CreateChannelModal
          onClose={() => setModal(null)}
          onCreated={(id) => {
            setModal(null);
            selectChannel(id);
          }}
        />
      )}
      {modal?.kind === "new-agent" && (
        <AddAgentModal state={state} channel={channel?.is_dm ? null : channel} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "agent-settings" && state.agents[modal.agentId] && (
        <AgentModal key={modal.agentId} state={state} agent={state.agents[modal.agentId]} onUpdated={agentUpdated} onClose={() => setModal(null)} />
      )}
      {modal?.kind === "plugins" && <PluginsModal state={state} onClose={() => { setModal(null); setPluginsRevision((value) => value + 1); }} />}
      {modal?.kind === "people" && <PeopleModal state={state} channelId={modal.channelId} personId={modal.personId} onClose={() => setModal(null)} onChannel={(next) => {
        dispatch({ type: "event", event: { type: "channel", channel: next } });
        setModal(null); selectChannel(next.id);
      }} />}
      {modal?.kind === "openai" && <ProvidersModal agents={Object.values(state.agents)} onClose={() => setModal(null)} />}
      {modal?.kind === "elements" && channel && <Suspense fallback={null}><ElementsPanel key={channel.id} channel={channel} state={state} open onClose={() => setModal(null)} /></Suspense>}
    </div>
  );
}
