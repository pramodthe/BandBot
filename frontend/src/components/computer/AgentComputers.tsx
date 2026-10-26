import { useEffect, useState } from "react";
import { ArrowUpRightIcon, LoaderCircleIcon, MonitorIcon } from "lucide-react";
import { api } from "@/api";
import type { Agent, AgentComputersResponse } from "@/types";
import ComputerPanel from "./ComputerPanel";
import { Button } from "@/components/ui/button";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function AgentComputers({ agent, preferredChannelId, onChannel, onMessage }: {
  agent: Agent;
  preferredChannelId: number | null;
  onChannel: (id: number) => void;
  onMessage: () => void;
}) {
  const [data, setData] = useState<AgentComputersResponse | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const result = await api<AgentComputersResponse>("GET", `/api/agents/${agent.id}/computers`);
        if (!stopped) { setData(result); setError(""); }
      } catch (cause) {
        if (!stopped) setError((cause as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 4000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [agent.id]);

  const computers = data?.computers ?? [];
  const preferred = computers.find((item) => item.channel_id === preferredChannelId);
  const selected = computers.find((item) => item.channel_id === selectedId)
    ?? (preferred?.task_id ? preferred : null)
    ?? computers.find((item) => item.agent_working)
    ?? computers.find((item) => item.status !== "not_started")
    ?? preferred ?? computers[0];
  const label = (item: typeof selected) => item ? item.is_dm ? "Direct chat" : `#${item.channel_name}` : "";
  const channelId = selected?.channel_id;
  const computerKey = selected ? `${selected.channel_id}-${selected.computer_key ?? selected.provider ?? "computer"}` : "";

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm">Watch {agent.name} work, open its files, or take over the desktop.</p>
        <Button variant="ghost" size="sm" onClick={() => channelId ? onChannel(channelId) : onMessage()}>
          Open chat<ArrowUpRightIcon data-icon="inline-end" />
        </Button>
      </div>
      <p className="text-muted-foreground text-xs">{selected?.shared
        ? "Workspace agents share this computer, its files, apps, and browser logins. Private chats have separate computers."
        : "This computer keeps its files and browser sessions within this agent and channel."}</p>
      {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
      {!data ? <p role="status" className="text-muted-foreground flex items-center gap-2 py-8 text-sm"><LoaderCircleIcon className="size-4 animate-spin motion-reduce:animate-none" />{error ? "Couldn’t load computers. Retrying…" : "Loading computers…"}</p> : <>
        {selected ? <div className="flex flex-wrap items-center gap-3">
          <Select value={String(selected.channel_id)} onValueChange={(value) => setSelectedId(Number(value))}>
            <SelectTrigger aria-label="Computer channel" className="w-full sm:w-64"><SelectValue /></SelectTrigger>
            <SelectContent><SelectGroup>{computers.map((item) => <SelectItem key={item.channel_id} value={String(item.channel_id)}>{label(item)}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>
        </div> : null}
        {!(selected?.configured ?? data.configured) ? <p className="text-muted-foreground text-sm">Computer access is not configured. A workspace admin needs to enable desktops.</p> : null}
        {selected?.task_id ? <ComputerPanel key={computerKey} computerKey={computerKey} embedded
          taskId={selected.task_id} agent={agent} working={selected.agent_working} channelId={selected.channel_id}
          liveBlocked={selected.can_control ? null : "Only workspace admins can use the live desktop in this channel."} /> : (
          <Empty className="rounded-xl border py-12">
            <EmptyHeader><EmptyMedia variant="icon"><MonitorIcon /></EmptyMedia><EmptyTitle>No computer started yet</EmptyTitle>
              <EmptyDescription>Ask {agent.name} to use the browser or desktop in {label(selected) || "a chat"}. Its screen will appear here when it starts.</EmptyDescription></EmptyHeader>
            <Button variant="outline" onClick={() => channelId ? onChannel(channelId) : onMessage()}>Open chat<ArrowUpRightIcon data-icon="inline-end" /></Button>
          </Empty>
        )}
      </>}
    </div>
  );
}
