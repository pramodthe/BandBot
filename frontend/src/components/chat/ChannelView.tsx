import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type Dispatch } from "react";
import { ArrowLeftIcon, LayoutDashboardIcon, MonitorIcon, PanelRightIcon, UsersIcon } from "lucide-react";
import { policyBlock, type State, type Action } from "@/store";
import type { Agent, Channel, ChannelComputer, Draft, Message } from "@/types";
import { cn, formatClock, parseUtc } from "@/lib/utils";
import Avatar from "@/components/shared/Avatar";
import Composer from "./Composer";
import TaskStarters from "./TaskStarters";
import { BrandMark } from "@/components/shared/Brand";
import { deliverMessage } from "./deliver";
import DocumentsPanel, { AttachmentCard } from "@/components/documents/DocumentsPanel";
import ComputerActivity from "@/components/computer/ComputerActivity";
import ImageGenerationActivity, { useImageGenerations } from "@/components/generated/ImageGenerationActivity";
import TaskPanel from "./TaskPanel";
import RichText, { MentionNames, SourcesPill, linksIn } from "@/components/shared/RichText";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import MessageComponents from "@/components/generated/MessageComponents";
import MessageArtifacts from "@/components/generated/MessageArtifacts";
import { isHtmlArtifact } from "@/components/generated/artifacts";
import ChannelViews from "@/components/generated/ChannelViews";
import ApprovalRequest from "./ApprovalRequest";
import WorkspaceEmpty from "@/components/shared/WorkspaceEmpty";
import { AgentHandoff } from "@/components/assistant-ui/elements/agent-handoff";
import type { Modal, OpenComputer } from "@/components/workspace/types";

const GROUP_WINDOW_MS = 5 * 60 * 1000;
const titleClass = "truncate text-base font-semibold";
const topicClass = "text-muted-foreground truncate text-[13px]";

function formatDay(iso: string) {
  const d = parseUtc(iso);
  const today = new Date();
  if (d.toDateString() === today.toDateString()) return "Today";
  return d.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
}

const isChatter = (m: Message) => m.author_type === "agent" && !m.to_humans;

function MessageContent({ message, settled, state }: { message: Message; settled: boolean; state: State }) {
  const targets = message.mentions.filter((m) => m.type !== "human").map((m) => m.name);
  if (message.approval) {
    const blocked = policyBlock(state, state.channels[message.channel_id], "approvers");
    return <ApprovalRequest approval={message.approval} agentName={message.author_name} blocked={blocked} />;
  }
  if (message.handoff && targets.length > 0) {
    const reason = message.content.replace(/^@\S+\s*/, "");
    return (
      <div className="pt-1">
        <AgentHandoff className="max-w-2xl" from={message.author_name} to={targets[0]} reason={reason} carried={[]} settled={settled} />
      </div>
    );
  }
  const links = message.author_type === "agent" && message.to_humans ? linksIn(message.content) : [];
  const hasWebpage = message.artifacts?.some((artifact) => isHtmlArtifact(artifact.path));
  const liveApps = message.components.filter((component) => component.kind === "app");
  const components = hasWebpage ? message.components.filter((component) => component.kind !== "app") : message.components;
  return (
    <>
      {isChatter(message) && targets.length > 0 && (
        <div className="text-chatter mb-0.5 text-xs font-bold">to {targets.join(", ")}</div>
      )}
      <RichText text={message.content} />
      {(message.artifacts?.length ?? 0) > 0 && <MessageArtifacts artifacts={message.artifacts!} liveApps={liveApps} />}
      {(message.attachments?.length ?? 0) > 0 && (
        <div className="my-2 flex flex-wrap gap-2">
          {message.attachments!.map((doc) => <AttachmentCard key={doc.id} document={doc} />)}
        </div>
      )}
      {components.length > 0 && (
        <MessageComponents
          components={components}
          channelId={message.channel_id}
          canRespond={state.channels[message.channel_id]?.human_ids.includes(state.me.id) ?? false}
        />
      )}
      {links.length > 1 && <SourcesPill links={links} />}
    </>
  );
}

const rowClass = "group mx-auto flex w-full max-w-[920px] gap-3 px-6 py-3 text-sm leading-relaxed max-sm:gap-2 max-sm:px-4";
const caretClass =
  "[&_[data-slot=rich-text]>:last-child>:last-child]:after:bg-muted-foreground [&_[data-slot=rich-text]>:last-child>:last-child]:after:animate-caret [&_[data-slot=rich-text]>:last-child>:last-child]:after:ms-0.5 [&_[data-slot=rich-text]>:last-child>:last-child]:after:inline-block [&_[data-slot=rich-text]>:last-child>:last-child]:after:h-[1em] [&_[data-slot=rich-text]>:last-child>:last-child]:after:w-[7px] [&_[data-slot=rich-text]>:last-child>:last-child]:after:align-text-bottom [&_[data-slot=rich-text]>:last-child>:last-child]:after:content-['']";

function AuthorLine({
  name,
  agent,
  tag,
  time,
  writing = false,
  mine = false,
  openModal,
}: {
  name: string;
  agent: Agent | null;
  tag: string | null;
  time: string;
  writing?: boolean;
  mine?: boolean;
  openModal: (m: Modal) => void;
}) {
  return (
    <div className="flex flex-wrap items-baseline gap-2">
      <button
        className="font-semibold enabled:hover:underline disabled:cursor-default"
        onClick={() => agent && openModal({ kind: "agent", agentId: agent.id })}
        disabled={!agent}
      >
        {name}
      </button>
      {tag && <Badge variant="tag" className="max-w-64 min-w-0 shrink" title={tag}><span className="truncate">{tag}</span></Badge>}
      <span className={cn("text-xs", mine ? "text-white/75" : "text-muted-foreground", writing && "italic")}>{time}</span>
    </div>
  );
}

function MessageRow({
  message,
  state,
  compact,
  settled,
  openModal,
}: {
  message: Message;
  state: State;
  compact: boolean;
  settled: boolean;
  openModal: (m: Modal) => void;
}) {
  const agent = message.author_type === "agent" && message.author_id != null ? state.agents[message.author_id] : null;
  const chatter = isChatter(message);
  const mine = message.author_type === "human" && message.author_id === state.me.id;

  return (
    <div
      id={`message-${message.id}`}
      tabIndex={-1}
      className={cn(rowClass, "rounded-xl outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-inset", compact && "py-1", mine && "flex-row-reverse")}
    >
      <div className="w-9 shrink-0">
        {compact ? (
          <span className="text-muted-foreground invisible block pt-[3px] text-right text-[11px] group-hover:visible">
            {formatClock(message.created_at)}
          </span>
        ) : agent ? (
          <button
            type="button"
            className="rounded-full outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`View ${agent.name} profile`}
            onClick={() => openModal({ kind: "agent", agentId: agent.id })}
          >
            <Avatar name={message.author_name} kind={message.author_type} role={agent.role} avatar={agent.avatar} runtime={agent.runtime} />
          </button>
        ) : (
          <Avatar name={message.author_name} kind={message.author_type} />
        )}
      </div>
      <div
        className={cn(
          "min-w-0 rounded-2xl px-4 py-3",
          mine ? "bg-self-message ml-auto max-w-[85%] text-white" : "bg-muted max-w-[calc(100%-3rem)]",
          chatter && "border-border border-l-2 text-muted-foreground",
          message.author_type === "system" && "text-muted-foreground italic",
          ((message.artifacts?.length ?? 0) > 0 || message.components.some((component) => component.kind === "app")) && "w-full",
        )}
      >
        {!compact && (
          <AuthorLine
            name={message.author_name}
            agent={agent}
            tag={agent ? agent.role_label : message.author_type === "external" ? "via Band" : null}
            time={formatClock(message.created_at)}
            openModal={openModal}
            mine={mine}
          />
        )}
        <MessageContent message={message} settled={settled} state={state} />
      </div>
    </div>
  );
}

function DraftRow({ draft, state, openModal }: { draft: Draft; state: State; openModal: (m: Modal) => void }) {
  const agent = state.agents[draft.agent_id];
  return (
    <div className={rowClass}>
      <div className="w-9 shrink-0">
        <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} runtime={agent.runtime} />
      </div>
      <div className={cn("min-w-0 flex-1", caretClass)}>
        <AuthorLine name={agent.name} agent={agent} tag={agent.role_label} time="writing…" writing openModal={openModal} />
        <RichText text={draft.content} streaming />
      </div>
    </div>
  );
}

export default function ChannelView({
  state,
  dispatch,
  channel,
  showMembers,
  onToggleMembers,
  onBack,
  openModal,
  computers,
  computerOpen,
  openComputer,
}: {
  state: State;
  dispatch: Dispatch<Action>;
  channel: Channel;
  showMembers: boolean;
  onToggleMembers: () => void;
  onBack: () => void;
  openModal: (m: Modal) => void;
  computers: ChannelComputer[];
  computerOpen: boolean;
  openComputer: OpenComputer;
}) {
  const [showChatter, setShowChatter] = useState(() => {
    try { return localStorage.getItem("show-chatter") === "1"; } catch { return false; }
  });
  const [viewsOpen, setViewsOpen] = useState(false);
  const [documentsOpen, setDocumentsOpen] = useState(false);
  const [suggestion, setSuggestion] = useState<{ text: string; revision: number }>();
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const messages = state.messages[channel.id];
  const imageGenerations = useImageGenerations(channel.id);
  const publishedImages = new Set((messages ?? []).flatMap((message) => message.components
    .filter((component) => component.kind === "image").map((component) => component.props.documentId)));
  const pendingImages = imageGenerations.filter((activity) => !activity.document_id || !publishedImages.has(activity.document_id));
  const imageProgress = pendingImages.map((activity) => `${activity.id}:${activity.status}`).join(",");
  const visible = (messages ?? []).filter((m) => showChatter || !isChatter(m) || !!m.approval || m.components?.some((component) => component.kind === "input"));
  const hiddenCount = (messages?.length ?? 0) - visible.length;
  const drafts = Object.values(state.drafts[channel.id] ?? {}).filter((d) => state.agents[d.agent_id]);
  const draftLength = drafts.reduce((total, d) => total + d.content.length, 0);
  // A hand-off is settled once its teammate has posted after it.
  const settledHandoffs = new Set<number>();
  const laterAuthors = new Set<string>();
  for (const m of [...(messages ?? [])].reverse()) {
    if (m.handoff && m.mentions.some((t) => laterAuthors.has(t.name))) settledHandoffs.add(m.id);
    laterAuthors.add(m.author_name);
  }

  useEffect(() => {
    try {
      localStorage.setItem("show-chatter", showChatter ? "1" : "0");
    } catch {
      // The current session still respects the selection.
    }
  }, [showChatter]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (el && pinned.current) el.scrollTop = el.scrollHeight;
  }, [visible.length, draftLength, imageProgress]);

  const onScroll = () => {
    const el = scroller.current;
    if (el) pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
  };

  const rows: ReactNode[] = [];
  let prev: Message | null = null;
  for (const m of visible) {
    const day = formatDay(m.created_at);
    if (!prev || formatDay(prev.created_at) !== day) {
      rows.push(
        <div
          key={`day-${m.id}`}
          className="before:border-border after:border-border mt-3.5 mb-1.5 flex items-center text-[13px] font-bold before:flex-1 before:border-t after:flex-1 after:border-t"
        >
          <span className="border-border rounded-xl border px-3 py-0.5">{day}</span>
        </div>,
      );
      prev = null;
    }
    const compact =
      !!prev &&
      prev.author_type === m.author_type &&
      prev.author_name === m.author_name &&
      isChatter(prev) === isChatter(m) &&
      parseUtc(m.created_at).getTime() - parseUtc(prev.created_at).getTime() < GROUP_WINDOW_MS;
    rows.push(
      <MessageRow key={m.id} message={m} state={state} compact={compact} settled={settledHandoffs.has(m.id)} openModal={openModal} />,
    );
    prev = m;
  }

  const mentionNames = [
    ...Object.values(state.agents).map((a) => a.name),
    ...Object.values(state.users).map((u) => u.display_name),
  ];
  const agentCount = channel.agent_ids.length;
  const humanCount = channel.human_ids.length;
  const dmPerson = state.users[channel.dm_user_ids?.find((id) => id !== state.me.id) ?? -1];
  const dmAgent = channel.dm_agent_id != null ? state.agents[channel.dm_agent_id] : null;
  const channelAgents = channel.agent_ids.map((id) => state.agents[id]).filter(Boolean);
  const starterAgent = dmAgent ?? channelAgents.find((agent) => agent.is_master) ?? channelAgents[0];
  const headerComputer = computers.find((c) => c.computer_working) ?? computers[0];
  const activeComputers = computers.filter((c) => c.computer_working);

  return (
    <section className="flex min-w-0 flex-1 flex-col">
      <header className="border-border flex min-h-20 flex-wrap items-center gap-x-3 gap-y-2 border-b px-6 py-4 max-sm:px-4">
        <Button variant="ghost" size="icon-sm" className="hidden max-sm:inline-flex" aria-label="Back to conversations" onClick={onBack}>
          <ArrowLeftIcon />
        </Button>
        {dmAgent ? (
          <button
            type="button"
            className="flex min-w-0 flex-1 items-center gap-2.5 rounded-lg text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`View ${dmAgent.name} profile`}
            onClick={() => openModal({ kind: "agent", agentId: dmAgent.id })}
          >
            <Avatar name={dmAgent.name} kind="agent" role={dmAgent.role} avatar={dmAgent.avatar} runtime={dmAgent.runtime} />
            <div className="min-w-0">
              <div className={titleClass}>{dmAgent.name}</div>
              <div className={topicClass}>
                {dmAgent.specialty || dmAgent.role_label} · Private conversation
              </div>
            </div>
          </button>
        ) : (
          <div className="min-w-0 flex-1">
            <div className={titleClass}>{dmPerson ? dmPerson.display_name : `# ${channel.name}`}</div>
            <div className={topicClass}>
              {dmPerson
                ? "Private conversation"
                : channel.topic || `${humanCount} ${humanCount === 1 ? "person" : "people"} · ${agentCount} AI ${agentCount === 1 ? "teammate" : "teammates"}`}
            </div>
          </div>
        )}
        <div className="ml-auto flex min-w-0 max-w-full shrink-0 flex-wrap items-center gap-2 max-sm:w-full">
          <Button variant="ghost" size="sm" onClick={() => setViewsOpen(true)}><LayoutDashboardIcon data-icon="inline-start" />Views</Button>
          <Button variant="ghost" size="sm" onClick={() => setDocumentsOpen(true)}>Files</Button>
          {!dmPerson && <label
            className="text-muted-foreground flex cursor-pointer items-center gap-1.5 text-xs"
            title="Show messages agents send to each other"
          >
            <Checkbox checked={showChatter} onCheckedChange={(checked) => setShowChatter(checked === true)} />
            Agent chatter
          </label>}
          {headerComputer && !computerOpen && (
            <Button
              variant="chip"
              size="chip"
              className={cn(headerComputer.computer_working && "border-[#1d9b5f] font-bold text-[#1d9b5f]")}
              title="Show the agent's computer"
              onClick={() => openComputer(headerComputer.agent_id, headerComputer.task_id)}
            >
              <MonitorIcon data-icon="inline-start" />Computer{headerComputer.computer_working ? " · live" : ""}
            </Button>
          )}
          {dmAgent ? (
            <Button variant="ghost" size="icon-sm" aria-label="Open agent profile" onClick={() => openModal({ kind: "agent", agentId: dmAgent.id })}>
              <PanelRightIcon />
            </Button>
          ) : !dmPerson ? (
            <Button
              variant="chip"
              size="chip"
              className={cn(showMembers && "bg-muted")}
              aria-pressed={showMembers}
              aria-label="Channel members"
              onClick={onToggleMembers}
            >
              <UsersIcon data-icon="inline-start" />{humanCount + agentCount}
            </Button>
          ) : null}
        </div>
      </header>
      <ChannelViews key={`views-${channel.id}`} channelId={channel.id} open={viewsOpen} onClose={() => setViewsOpen(false)} />
      {documentsOpen && (
        <DocumentsPanel key={`documents-${channel.id}`} channelId={channel.id} state={state} onClose={() => setDocumentsOpen(false)} />
      )}

      <div
        className={cn("min-h-0 flex-1 overflow-y-auto pt-3 pb-1", messages?.length === 0 && pendingImages.length === 0 && "flex flex-col")}
        ref={scroller}
        onScroll={onScroll}
      >
        {!messages && <div className="text-muted-foreground p-6 text-center">Loading messages…</div>}
        {messages && messages.length === 0 && pendingImages.length === 0 && (
          <WorkspaceEmpty
            title={dmPerson ? `Chat with ${dmPerson.display_name}` : dmAgent ? `A little help from ${dmAgent.name}` : `Welcome to #${channel.name}`}
            description={dmPerson
              ? "Your private conversation. Send a message to get started."
              : dmAgent
                ? `Share a goal with ${dmAgent.name}. Work through it together, right here.`
                : starterAgent
                  ? `Share an idea, make a plan, or hand off a task. Mention @${starterAgent.name} to bring your AI teammates into the conversation.`
                  : "A shared space for your people and ideas. Add an AI teammate when you’re ready to put them to work."}
            icon={!dmPerson ? (
              <div className="mb-2 flex items-center gap-2">
                {starterAgent ? (
                  <Avatar name={starterAgent.name} kind="agent" role={starterAgent.role} avatar={starterAgent.avatar} runtime={starterAgent.runtime} size={64} />
                ) : (
                  <BrandMark className="size-14" />
                )}
              </div>
            ) : undefined}
          >
            {!dmPerson && starterAgent && (
              <TaskStarters
                agentName={starterAgent.name}
                isDm={channel.is_dm}
                onPick={(text) => setSuggestion((current) => ({ text, revision: (current?.revision ?? 0) + 1 }))}
              />
            )}
            {!dmPerson && !starterAgent && (
              <Button variant="outline" className="rounded-xl" onClick={() => openModal({ kind: "new-agent" })}><UsersIcon />Add a teammate</Button>
            )}
          </WorkspaceEmpty>
        )}
        <MentionNames names={mentionNames}>
          {rows}
          {(state.outgoing[channel.id] ?? []).filter((item) => !messages?.some((message) => message.client_id === item.clientId)).map((item) => (
            <div key={item.clientId} className={cn(rowClass, "flex-row-reverse")}>
              <div className="w-9 shrink-0"><Avatar name={state.me.display_name} kind="human" /></div>
              <div className="bg-self-message ml-auto max-w-[85%] rounded-2xl px-4 py-3 text-white">
                <AuthorLine
                  name={state.me.display_name}
                  agent={null}
                  tag={null}
                  time={item.status === "pending" ? "Sending…" : "Not sent"}
                  mine
                  openModal={openModal}
                />
                {item.content ? (
                  <p className="whitespace-pre-wrap">{item.content}</p>
                ) : (
                  <p className="text-white/80">{item.attachmentIds.length} attachment{item.attachmentIds.length === 1 ? "" : "s"}</p>
                )}
                {item.status === "pending" && <p className="mt-1 text-xs text-white/75" role="status">Sending…</p>}
                {item.status === "failed" && (
                  <div className="mt-2">
                    <p className="text-xs text-white/90" role="alert">{item.error || "Couldn't send"}</p>
                    {item.retryable && (
                      <Button
                        variant="outline"
                        size="sm"
                        className="mt-2 border-white/40 bg-transparent text-white hover:bg-white/10 hover:text-white"
                        aria-label="Retry sending"
                        onClick={() => void deliverMessage(dispatch, channel.id, item.content, item.attachmentIds, item.clientId)}
                      >
                        Retry
                      </Button>
                    )}
                  </div>
                )}
              </div>
            </div>
          ))}
          {drafts.map((d) => (
            <DraftRow key={d.task_id} draft={d} state={state} openModal={openModal} />
          ))}
        </MentionNames>
        {pendingImages.map((activity) => state.agents[activity.agent_id] && (
          <ImageGenerationActivity key={activity.id} activity={activity} agent={state.agents[activity.agent_id]} />
        ))}
        {!computerOpen && activeComputers.map((activity) => state.agents[activity.agent_id] && (
          <ComputerActivity
            key={`${channel.id}-${activity.task_id}`}
            agent={state.agents[activity.agent_id]}
            activity={activity}
            openComputer={openComputer}
          />
        ))}
        {hiddenCount > 0 && (
          <Button variant="link" size="inline" className="mx-5 my-2 text-[13px]" onClick={() => setShowChatter(true)}>
            {hiddenCount} agent-to-agent message{hiddenCount === 1 ? "" : "s"} hidden. Show them
          </Button>
        )}
      </div>

      {!dmPerson && (
        <TaskPanel key={`tasks-${channel.id}`} channelId={channel.id} state={state} dispatch={dispatch} openComputer={openComputer} />
      )}

      {state.serviceStatus?.worker === "unavailable" && (
        <p role="status" className="text-muted-foreground px-5 pb-2 text-sm">
          Worker unavailable. Messages to agents are saved and will run when the worker is back.
        </p>
      )}
      {state.serviceStatus?.providers.filter((provider) => provider.state === "sign_in_required").map((provider) => (
        <div key={provider.agent_id} role="status" className="mx-5 mb-2 flex flex-wrap items-center gap-2 rounded-xl border px-3 py-2 text-sm">
          <span className="min-w-0 flex-1">{provider.action === "reconnect"
            ? `${provider.name} needs you to sign in again.`
            : `Provider sign-in required for ${provider.name}. The connection owner can reconnect it.`}</span>
          {provider.action === "reconnect" && (
            <Button size="sm" variant="outline" aria-label="Reconnect provider" onClick={() => openModal({ kind: "new-agent" })}>Reconnect provider</Button>
          )}
        </div>
      ))}
      {channel.messaging_disabled ? (
        <p role="status" className="text-muted-foreground px-5 py-4 text-sm">Messaging isn't available for this conversation. History is unchanged.</p>
      ) : (
        <Composer state={state} channel={channel} dispatch={dispatch} suggestion={suggestion} />
      )}
    </section>
  );
}
