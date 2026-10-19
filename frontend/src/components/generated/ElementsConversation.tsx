import { createContext, useContext, useMemo, useState } from "react";
import { AssistantRuntimeProvider, useAuiState, useExternalStoreRuntime, WebSpeechDictationAdapter, WebSpeechSynthesisAdapter, type ThreadMessageLike } from "@assistant-ui/react";
import { api } from "@/api";
import { policyBlock, type State } from "@/store";
import type { Channel, Message } from "@/types";
import { Thread } from "@/components/assistant-ui/elements/thread.aui";
import ApprovalRequest from "@/components/chat/ApprovalRequest";
import { AttachmentCard } from "@/components/documents/DocumentsPanel";
import RichText from "@/components/shared/RichText";
import MessageComponents from "./MessageComponents";
import MessageArtifacts from "./MessageArtifacts";

const SourceContext = createContext<{ state: State; channel: Channel } | null>(null);

function BandMessage() {
  const source = useContext(SourceContext)!;
  const id = useAuiState((value) => value.message.id);
  const message = source.state.messages[source.channel.id]?.find((row) => String(row.id) === id);
  const draft = Object.values(source.state.drafts[source.channel.id] ?? {}).find((row) => `draft-${row.task_id}` === id);
  if (!message) return <div className="px-4 py-3"><p className="mb-1 text-sm font-semibold">{draft ? source.state.agents[draft.agent_id]?.name : "Agent"}</p><RichText text={draft?.content ?? ""} /></div>;
  return <div className="flex min-w-0 flex-col gap-2 px-4 py-3 text-sm">
    <div className="flex flex-wrap items-center gap-2"><span className="font-semibold">{message.author_name}</span><time className="text-muted-foreground text-xs" dateTime={message.created_at}>{new Date(message.created_at).toLocaleTimeString()}</time></div>
    {message.approval ? <ApprovalRequest approval={message.approval} agentName={message.author_name} blocked={policyBlock(source.state, source.channel, "approvers")} /> : <RichText text={message.content} />}
    {message.components?.length > 0 && <MessageComponents components={message.components} channelId={source.channel.id} canRespond={source.channel.human_ids.includes(source.state.me.id)} />}
    {!!message.attachments?.length && <div className="flex flex-wrap gap-2">{message.attachments.map((document) => <AttachmentCard key={document.id} document={document} />)}</div>}
    {!!message.artifacts?.length && <MessageArtifacts artifacts={message.artifacts} />}
  </div>;
}

const components = { AssistantMessage: BandMessage, UserMessage: BandMessage };

export default function ElementsConversation({ state, channel }: { state: State; channel: Channel }) {
  const [error, setError] = useState("");
  const rows = state.messages[channel.id] ?? [];
  const drafts = Object.values(state.drafts[channel.id] ?? {});
  const activity = Object.values(state.activity[channel.id] ?? {});
  const messages: ThreadMessageLike[] = [
    ...rows.filter((message: Message) => message.to_humans || message.author_type === "human" || !!message.approval || message.components?.some((component) => component.kind === "input")).map((message) => ({
      id: String(message.id), role: message.author_type === "human" && message.author_id === state.me.id ? "user" as const : "assistant" as const,
      content: message.content, createdAt: new Date(message.created_at),
    })),
    ...drafts.map((draft) => ({ id: `draft-${draft.task_id}`, role: "assistant" as const, content: draft.content, status: { type: "running" as const } })),
  ];
  const adapters = useMemo(() => ({
    speech: typeof window !== "undefined" && "speechSynthesis" in window ? new WebSpeechSynthesisAdapter() : undefined,
    dictation: typeof window !== "undefined" && ("SpeechRecognition" in window || "webkitSpeechRecognition" in window) ? new WebSpeechDictationAdapter() : undefined,
  }), []);
  const runtime = useExternalStoreRuntime({
    messages,
    convertMessage: (message) => message,
    isLoading: state.messages[channel.id] === undefined,
    isDisabled: !channel.human_ids.includes(state.me.id),
    isRunning: drafts.length > 0 || activity.some((task) => task.status === "running"),
    adapters,
    onNew: async (message) => {
      const content = message.content.filter((part) => part.type === "text").map((part) => part.text).join("\n").trim();
      if (!content) return;
      setError("");
      try { await api("POST", `/api/channels/${channel.id}/messages`, { content }); }
      catch (cause) { setError((cause as Error).message); throw cause; }
    },
    onCancel: async () => {
      const results = await Promise.allSettled(activity.filter((task) => task.status === "running").map((task) => api("POST", `/api/tasks/${task.id}/cancel`)));
      const failure = results.find((result) => result.status === "rejected");
      if (failure?.status === "rejected") { setError((failure.reason as Error).message); throw failure.reason; }
    },
  });
  return <SourceContext.Provider value={{ state, channel }}><AssistantRuntimeProvider runtime={runtime}><div className="flex h-full min-h-0 flex-col">
    {error && <p role="alert" className="text-destructive px-4 py-2 text-sm">{error}</p>}
    <Thread components={components} autoFocus={false} />
  </div></AssistantRuntimeProvider></SourceContext.Provider>;
}
