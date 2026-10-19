import { lazy, Suspense, useEffect, useState } from "react";
import { api } from "@/api";
import type { State } from "@/store";
import type { AssistantUISurface, Channel, ChannelUsage, Schedule, Task, TaskDetail } from "@/types";
import { formatCost } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { TaskCard } from "@/components/assistant-ui/elements/task-card";
import { CostMeter } from "@/components/assistant-ui/elements/cost-meter";
import { BackgroundInbox } from "@/components/assistant-ui/elements/background-inbox";
import { NumberTicker } from "@/components/assistant-ui/elements/number-ticker";
import RichText from "@/components/shared/RichText";

const AssistantUIBody = lazy(() => import("./AssistantUI").then((m) => ({ default: m.AssistantUIBody })));
const ElementsConversation = lazy(() => import("./ElementsConversation"));

interface Entry { id: string; name: string; category: string; source: string; files: string[] }
interface Catalog { elements: Entry[]; examples: Record<string, AssistantUISurface & { source: string }>; components: Record<string, unknown> }
const taskState = { queued: "waiting", running: "working", waiting: "waiting", completed: "done", failed: "failed", cancelled: "cancelled" } as const;

function Activity({ channel, state }: { channel: Channel; state: State }) {
  const [tasks, setTasks] = useState<Task[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [usage, setUsage] = useState<ChannelUsage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let stopped = false;
    let timer: number;
    const poll = async () => {
      try {
        const [rows, scheduled, totals] = await Promise.all([
          api<Task[]>("GET", `/api/channels/${channel.id}/tasks`),
          api<Schedule[]>("GET", `/api/channels/${channel.id}/schedules`),
          api<ChannelUsage>("GET", `/api/channels/${channel.id}/usage`),
        ]);
        if (stopped) return;
        setTasks(rows); setSchedules(scheduled); setUsage(totals); setError("");
      } catch (cause) { if (!stopped) setError((cause as Error).message); }
      finally { if (!stopped) timer = window.setTimeout(poll, 3000); }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channel.id, revision]);
  const control = async (id: string, action: "cancel" | "retry") => {
    if (busy) return;
    setBusy(id);
    try { await api("POST", `/api/tasks/${id}/${action}`); setRevision((value) => value + 1); }
    catch (cause) { setError((cause as Error).message); }
    finally { setBusy(null); }
  };
  const read = async (id: string) => {
    try { setDetail(await api<TaskDetail>("GET", `/api/tasks/${id}`)); }
    catch (cause) { setError((cause as Error).message); }
  };
  return <div className="flex flex-col gap-5">
    {error && <p role="alert" className="text-destructive">{error}</p>}
    <div className="grid gap-5 sm:grid-cols-2">
      <NumberTicker value={tasks.filter((task) => task.status === "running").length} label="Active tasks" />
      {usage && <div className="flex flex-col gap-2"><CostMeter runCost={usage.cost === null ? "Cost unavailable" : formatCost(usage.cost)} runLabel="channel cost" sessionCost="" sessionLabel="" lines={[]} /><p className="text-muted-foreground text-sm">{usage.tokens.toLocaleString()} tokens since {new Date(usage.since).toLocaleString()}</p></div>}
    </div>
    <BackgroundInbox runs={tasks.filter((task) => task.status === "running" || task.status === "completed" || task.status === "failed").map((task) => ({ id: task.id, title: `${state.agents[task.agent_id]?.name ?? "Agent"}: ${task.progress}`, state: task.status === "completed" ? "ready" : task.status === "failed" ? "failed" : "running", elapsed: new Date(task.updated_at).toLocaleTimeString() }))} onCollect={(id) => void read(id)} />
    {detail && <section aria-label="Task result" className="border-border rounded-lg border p-4"><div className="mb-3 flex items-center justify-between gap-3"><h3 className="font-semibold">Task result</h3><Button variant="ghost" size="sm" onClick={() => setDetail(null)}>Close result</Button></div><RichText text={detail.result || detail.progress} /></section>}
    {tasks.map((task) => <TaskCard key={task.id} label={state.agents[task.agent_id]?.name ?? "Agent"} meta={task.progress} state={taskState[task.status]} result={task.status === "failed" ? task.error : undefined} actions={<>
      <Button variant="ghost" size="sm" onClick={() => void read(task.id)}>Details</Button>
      {(task.status === "running" || task.status === "queued" || task.status === "waiting") && <Button variant="ghost" size="sm" disabled={busy === task.id || task.cancel_requested} onClick={() => void control(task.id, "cancel")}>Stop</Button>}
      {(task.status === "failed" || task.status === "cancelled") && <Button variant="ghost" size="sm" disabled={busy === task.id} onClick={() => void control(task.id, "retry")}>Retry</Button>}
    </>} />)}
    {schedules.length > 0 && <section className="flex flex-col gap-3"><h3 className="font-semibold">Scheduled work</h3>{schedules.map((schedule) => <div key={schedule.id} className="border-border flex items-center justify-between gap-3 rounded-lg border p-3"><div><p>{schedule.prompt}</p><p className="text-muted-foreground text-sm">{schedule.when} · {schedule.next_run_at ? new Date(schedule.next_run_at).toLocaleString() : "No next run"}</p></div><Button variant="ghost" size="sm" disabled={busy === `schedule-${schedule.id}`} onClick={async () => {
      if (busy) return;
      setBusy(`schedule-${schedule.id}`);
      try { await api("DELETE", `/api/channels/${channel.id}/schedules/${schedule.id}`); setRevision((value) => value + 1); }
      catch (cause) { setError((cause as Error).message); }
      finally { setBusy(null); }
    }}>Cancel schedule</Button></div>)}</section>}
    {tasks.length === 0 && <p className="text-muted-foreground">Mention an agent in this channel to start work.</p>}
  </div>;
}

export default function ElementsPanel({ channel, state, open, onClose }: { channel: Channel; state: State; open: boolean; onClose: () => void }) {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  useEffect(() => {
    if (!open) return;
    let stopped = false;
    api<Catalog>("GET", `/api/channels/${channel.id}/elements`).then((value) => { if (!stopped) { setCatalog(value); setError(""); } }).catch((cause) => { if (!stopped) setError((cause as Error).message); });
    return () => { stopped = true; };
  }, [channel.id, open]);
  const example = selected ? catalog?.examples[selected] : null;
  return <Dialog open={open} onOpenChange={(next) => !next && onClose()}><DialogContent className="flex h-[90dvh] max-h-[90dvh] flex-col overflow-hidden sm:max-w-6xl"><DialogHeader><DialogTitle>Workspace interfaces</DialogTitle><DialogDescription>Channel activity, conversation, and the installed Assistant UI collection.</DialogDescription></DialogHeader>
    <Tabs defaultValue="activity" className="flex min-h-0 flex-1 flex-col"><TabsList className="w-full shrink-0"><TabsTrigger value="activity">Activity</TabsTrigger><TabsTrigger value="conversation">Conversation</TabsTrigger><TabsTrigger value="examples">Examples</TabsTrigger><TabsTrigger value="library">Library {catalog?.elements.length ?? ""}</TabsTrigger></TabsList>
      <TabsContent value="activity" className="min-h-0 overflow-y-auto p-2"><Activity channel={channel} state={state} /></TabsContent>
      <TabsContent value="conversation" className="min-h-0 flex-1 overflow-hidden"><Suspense fallback={<p role="status">Loading conversation…</p>}><ElementsConversation channel={channel} state={state} /></Suspense></TabsContent>
      <TabsContent value="examples" className="min-h-0 overflow-y-auto p-2">
        <p className="text-muted-foreground mb-4 text-sm">22 upstream compositions using sample data. Agents can discover these through ui_elements.</p>
        <Select value={selected ?? ""} onValueChange={setSelected}>
          <SelectTrigger aria-label="Choose an example" className="w-full sm:max-w-sm"><SelectValue placeholder="Choose an example" /></SelectTrigger>
          <SelectContent><SelectGroup>{Object.entries(catalog?.examples ?? {}).map(([id, value]) => <SelectItem key={id} value={id}>{value.title}</SelectItem>)}</SelectGroup></SelectContent>
        </Select>
        {example && <section className="mt-6 flex flex-col gap-3">
          <p className="text-muted-foreground text-sm">Example data · <a href={example.source} target="_blank" rel="noreferrer" className="underline">Upstream documentation</a></p>
          <fieldset disabled className="min-w-0"><Suspense fallback={<p role="status">Loading example…</p>}><AssistantUIBody surface={example} /></Suspense></fieldset>
        </section>}
      </TabsContent>
      <TabsContent value="library" className="min-h-0 overflow-y-auto p-2"><Input aria-label="Filter interface library" placeholder="Find an element or category…" value={query} onChange={(event) => setQuery(event.target.value)} /><p className="text-muted-foreground my-3 text-sm">Component source is installed locally. App controls use host state; agent output uses the validated ui_catalog vocabulary.</p>{error && <p role="alert" className="text-destructive">{error}</p>}<div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">{catalog?.elements.filter((entry) => `${entry.name} ${entry.category}`.toLowerCase().includes(query.toLowerCase())).map((entry) => <a key={entry.id} href={entry.source} target="_blank" rel="noreferrer" className="border-border hover:bg-muted flex flex-col gap-1 rounded-lg border p-3"><span className="font-medium">{entry.name}</span><span className="text-muted-foreground text-xs">{entry.category} · {entry.category === "Generative" ? "Composition example" : "Component source installed"}</span></a>)}</div></TabsContent>
    </Tabs>
  </DialogContent></Dialog>;
}
