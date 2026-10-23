import { useEffect, useRef, useState, type Dispatch, type ReactNode } from "react";
import {
  BanIcon,
  CalendarClockIcon,
  CircleAlertIcon,
  CircleCheckIcon,
  ClockIcon,
  ChevronDownIcon,
  EllipsisIcon,
  HandIcon,
  LoaderCircleIcon,
  XIcon,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/api";
import { isAdmin, policyBlock, type State, type Action } from "@/store";
import type { Memory, Message, Schedule, Task, TaskDetail } from "@/types";
import type { OpenComputer } from "@/components/workspace/types";
import RichText from "@/components/shared/RichText";
import Avatar from "@/components/shared/Avatar";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { cn, formatCost, formatTokens, parseUtc } from "@/lib/utils";
import ActivityLog from "@/components/shared/ActivityLog";
import { ArtifactCard } from "@/components/assistant-ui/elements/artifact-card";
import { MemoryChips } from "@/components/assistant-ui/elements/memory-chips";
import { ToolError } from "@/components/assistant-ui/elements/tool-error";
import type { TimelineStep } from "@/components/assistant-ui/elements/tool-timeline";
import ArtifactPreview from "@/components/generated/ArtifactPreview";
import { downloadArtifact, isHtmlArtifact } from "@/components/generated/artifacts";

const ICONS: Record<Task["status"], LucideIcon> = {
  queued: ClockIcon,
  running: LoaderCircleIcon,
  waiting: HandIcon,
  completed: CircleCheckIcon,
  failed: CircleAlertIcon,
  cancelled: BanIcon,
};

const FINISHED_SHOWN = 10;

const isActive = (t: Task) => t.status === "queued" || t.status === "running" || t.status === "waiting";

function since(iso: string) {
  const seconds = Math.max(0, (Date.now() - parseUtc(iso).getTime()) / 1000);
  if (seconds < 60) return "now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
  return parseUtc(iso).toLocaleDateString([], { month: "short", day: "numeric" });
}

const clip = (text: string, max = 80) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);
const words = (text: string) => text.split(/\s+/).filter(Boolean).length;
const byTime = (key: "created_at" | "updated_at") => (a: Task, b: Task) =>
  parseUtc(a[key]).getTime() - parseUtc(b[key]).getTime();

function usageFor(task: Task) {
  if (!task.usage) return "";
  const tokens = `${formatTokens(task.usage.input_tokens + task.usage.output_tokens)} tokens`;
  return task.usage.cost != null ? `${tokens} (${formatCost(task.usage.cost)})` : tokens;
}

function verbFor(task: Task) {
  if (task.status === "running" && task.cancel_requested) return "Cancelling…";
  if (task.provider_block) return "Needs connection";
  if (task.status === "failed") return "Failed";
  if (task.status === "cancelled") return "Cancelled";
  return clip(task.progress);
}

function TaskResult({ detail }: { detail: TaskDetail }) {
  return (
    <>
      <div className="max-h-64 overflow-y-auto text-[13px]">
        <RichText text={detail.result} />
      </div>
      {detail.artifacts.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {detail.artifacts.map((artifact) => (
            isHtmlArtifact(artifact.path) ? <div key={artifact.path} className="w-full"><ArtifactPreview artifact={artifact} /></div> :
            <ArtifactCard
              key={artifact.path}
              role="button"
              tabIndex={0}
              aria-label={`Download ${artifact.path}`}
              title={artifact.path.split("/").pop() || artifact.path}
              meta={`${artifact.path} · ${words(artifact.content)} words`}
              onClick={() => downloadArtifact(artifact)}
              onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && downloadArtifact(artifact)}
            />
          ))}
        </div>
      )}
    </>
  );
}

function RowMenu({ label, children }: { label: string; children: ReactNode }) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon-sm" className="text-muted-foreground" aria-label={`Actions for ${label}`}>
          <EllipsisIcon />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="min-w-40">
        {children}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

function Box({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  return (
    <div className="border-border flex flex-col gap-2 rounded-xl border p-3">
      <div className="flex items-center gap-2">
        <span className="flex-1">{title}</span>
        <Button variant="ghost" size="icon-xs" aria-label="Close" onClick={onClose}>
          <XIcon />
        </Button>
      </div>
      {children}
    </div>
  );
}

export default function TaskPanel({ channelId, state, dispatch, openComputer }: {
  channelId: number; state: State; dispatch: Dispatch<Action>; openComputer: OpenComputer;
}) {
  const [expanded, setExpanded] = useState(false);
  const [tasks, setTasks] = useState<Task[]>([]);
  const [schedules, setSchedules] = useState<Schedule[]>([]);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [results, setResults] = useState<Record<string, TaskDetail>>({});
  const [memory, setMemory] = useState<{ agentId: number; rows: Memory[] } | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [activity, setActivity] = useState<{ taskId?: string; agentId?: number } | null>(null);
  const version = useRef("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    version.current = "";
    setTasks([]);
    setSchedules([]);
    setResults({});
    setMemory(null);
    setDetailId(null);
    setActivity(null);
    const poll = async () => {
      try {
        const [rows, planned] = await Promise.all([
          api<Task[]>("GET", `/api/channels/${channelId}/tasks`),
          api<Schedule[]>("GET", `/api/channels/${channelId}/schedules`),
        ]);
        if (stopped) return;
        setTasks(rows);
        setSchedules(planned);
        dispatch({ type: "tasks_loaded", channelId, tasks: rows.map((t) => ({
          id: t.id, channel_id: channelId, agent_id: t.agent_id,
          status: isActive(t) ? t.status as "queued" | "running" | "waiting" : "done",
          progress: t.progress,
        })) });
        setError("");
        const next = rows.filter((t) => t.status === "completed").map((t) => t.id).join(",");
        if (next !== version.current) {
          const messages = await api<Message[]>("GET", `/api/channels/${channelId}/messages`);
          if (stopped) return;
          dispatch({ type: "messages_loaded", channelId, messages });
          version.current = next;
        }
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 2000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId, dispatch]);

  const control = async (id: string, action: "cancel" | "retry") => {
    setBusy(id);
    try {
      const updated = await api<Task>("POST", `/api/tasks/${id}/${action}`);
      setTasks((rows) => rows.map((t) => t.id === id ? updated : t));
      setError("");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(null); }
  };

  const restartWithProviders = async (id: string) => {
    setBusy(id);
    try {
      const replacement = await api<Task>("POST", `/api/tasks/${id}/restart-with-providers`);
      setTasks((rows) => rows.some((t) => t.id === replacement.id) ? rows.map((t) => t.id === replacement.id ? replacement : t) : [...rows, replacement]); setError("");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(null); }
  };

  const openOriginal = async (id: string) => {
    try {
      const original = await api<TaskDetail>("GET", `/api/tasks/${id}`);
      setTasks((rows) => rows.some((t) => t.id === id) ? rows : [...rows, original]);
      setResults((rows) => ({ ...rows, [id]: original })); setDetailId(id);
    } catch (err) { setError((err as Error).message); }
  };

  const cancelSchedule = async (schedule: Schedule) => {
    try {
      await api("DELETE", `/api/channels/${channelId}/schedules/${schedule.id}`);
      setSchedules((rows) => rows.filter((s) => s.id !== schedule.id));
      setError("");
    } catch (err) { setError((err as Error).message); }
  };

  const loadResult = async (id: string) => {
    if (results[id]) return;
    try {
      const detail = await api<TaskDetail>("GET", `/api/tasks/${id}`);
      setResults((current) => ({ ...current, [id]: detail }));
    } catch (err) { setError((err as Error).message); }
  };

  const showDetail = (task: Task) => {
    setExpanded(true);
    setDetailId(task.id);
    if (task.status === "completed") void loadResult(task.id);
  };

  const showMemory = async (agentId: number) => {
    try {
      const rows = await api<Memory[]>("GET", `/api/channels/${channelId}/agents/${agentId}/memories`);
      setMemory({ agentId, rows });
      setExpanded(true);
    } catch (err) { setError((err as Error).message); }
  };

  const forget = async (agentId: number, key: string) => {
    try {
      await api("DELETE", `/api/channels/${channelId}/agents/${agentId}/memories/${encodeURIComponent(key)}`);
      await showMemory(agentId);
    } catch (err) { setError((err as Error).message); }
  };

  const agentName = (id: number) => state.agents[id]?.name ?? "Agent";
  const active = tasks.filter((t) => t.status === "running" || t.status === "queued").length;
  const waiting = tasks.filter((t) => t.status === "waiting").length;
  const summary = [
    active > 0 && `${active} active`,
    waiting > 0 && `${waiting} ${waiting === 1 ? "needs" : "need"} attention`,
    schedules.length > 0 && `${schedules.length} scheduled`,
  ].filter(Boolean).join(" · ") || "No active tasks";

  const finished = tasks.filter((t) => !isActive(t)).sort(byTime("updated_at")).slice(-FINISHED_SHOWN);
  const pending = tasks.filter((t) => t.status === "queued" || t.status === "waiting").sort(byTime("created_at"));
  const running = tasks.filter((t) => t.status === "running").sort(byTime("created_at"));
  const detail = tasks.find((t) => t.id === detailId);
  const current = [...pending.filter((t) => t.status === "waiting"), ...running, ...pending.filter((t) => t.status === "queued")];
  const shownTasks = expanded ? [...current, ...[...finished].reverse()] : current.length > 0 ? current.slice(0, 2) : finished.slice(-1);

  const requestFor = (task: Task) => (state.messages[channelId] ?? []).find((message) =>
    message.author_type === "agent" && message.author_id === task.agent_id && (message.approval?.status === "pending"
      || message.components.some((component) => component.kind === "input" && component.request.taskId === task.id && component.request.status === "pending")),
  );

  const reviewRequest = (task: Task) => {
    const request = requestFor(task);
    const element = request && document.getElementById(`message-${request.id}`);
    element?.scrollIntoView({ block: "center" });
    element?.focus({ preventScroll: true });
  };

  const taskStep = (task: Task): TimelineStep => {
    const verb = verbFor(task);
    return {
      id: task.id,
      icon: ICONS[task.status],
      verb,
      chip: [agentName(task.agent_id), since(task.updated_at), usageFor(task)].filter(Boolean).join(" · "),
      action: (
        <div className="flex flex-wrap items-center justify-end gap-1">
          {task.status === "waiting" && requestFor(task) ? <Button variant="outline" size="sm" className="text-xs" onClick={() => reviewRequest(task)}>Review request</Button> : null}
          {task.status === "completed" ? <Button variant="ghost" size="sm" className="text-xs" onClick={() => showDetail(task)}>View result</Button> : null}
          {task.status === "failed" ? <Button variant="ghost" size="sm" className="text-xs" disabled={busy === task.id} onClick={() => void control(task.id, "retry")}>Retry</Button> : null}
          {task.status === "running" || task.status === "queued" ? <Button variant="ghost" size="sm" className="text-xs" disabled={busy === task.id || task.cancel_requested} onClick={() => void control(task.id, "cancel")}>{task.cancel_requested ? "Stopping…" : "Stop"}</Button> : null}
          {task.provider_block && isAdmin(state) && <Button variant="ghost" size="sm" onClick={() => window.dispatchEvent(new Event("open-provider-settings"))}>Reconnect</Button>}
          {task.provider_block && <Button variant="ghost" size="sm" disabled={busy === task.id} onClick={() => void restartWithProviders(task.id)}>New task with current providers</Button>}
          <RowMenu label={`${agentName(task.agent_id)}: ${verb}`}>
            {task.restarted_from && <DropdownMenuItem onSelect={() => void openOriginal(task.restarted_from!)}>View original task</DropdownMenuItem>}
            {task.status === "completed" && <DropdownMenuItem onSelect={() => showDetail(task)}>View result</DropdownMenuItem>}
            {task.status === "failed" && <DropdownMenuItem onSelect={() => showDetail(task)}>View error</DropdownMenuItem>}
            {task.status === "failed" && (
              <DropdownMenuItem disabled={busy === task.id} onSelect={() => void control(task.id, "retry")}>Retry</DropdownMenuItem>
            )}
            {task.status === "cancelled" && (
              <DropdownMenuItem disabled={busy === task.id} onSelect={() => void control(task.id, "retry")}>Resume</DropdownMenuItem>
            )}
            {isActive(task) && (
              <DropdownMenuItem
                variant="destructive"
                disabled={busy === task.id || task.cancel_requested
                  || (task.status === "waiting" && task.waiting_for !== "input" && !!policyBlock(state, state.channels[channelId], "approvers"))}
                onSelect={() => void control(task.id, "cancel")}
              >
                Cancel task
              </DropdownMenuItem>
            )}
            <DropdownMenuItem onSelect={() => { setExpanded(true); setActivity({ taskId: task.id, agentId: task.agent_id }); }}>View activity</DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem onSelect={() => openComputer(task.agent_id, task.id)}>Open computer</DropdownMenuItem>
            <DropdownMenuItem onSelect={() => void showMemory(task.agent_id)}>Show memory</DropdownMenuItem>
          </RowMenu>
        </div>
      ),
    };
  };

  const scheduleStep = (schedule: Schedule): TimelineStep => {
    const next = schedule.next_run_at
      ? `next ${parseUtc(schedule.next_run_at).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" })}`
      : "not scheduled";
    return {
      id: `schedule-${schedule.id}`,
      icon: CalendarClockIcon,
      verb: clip(`${schedule.when.charAt(0).toUpperCase()}${schedule.when.slice(1)}: ${schedule.prompt}`),
      chip: `${agentName(schedule.agent_id)} · ${next}`,
      action: (
        <RowMenu label={`${agentName(schedule.agent_id)} schedule`}>
          <DropdownMenuItem variant="destructive" onSelect={() => void cancelSchedule(schedule)}>Cancel schedule</DropdownMenuItem>
        </RowMenu>
      ),
    };
  };

  return (
    <section aria-label="Agent tasks" className={cn("mx-6 mb-3 flex max-h-[36vh] min-h-0 shrink-0 flex-col overflow-y-auto rounded-2xl border border-border bg-sidebar text-[13px] max-sm:mx-4", waiting > 0 && "border-warning-foreground/35")}>
      <button type="button" aria-expanded={expanded} aria-controls={`task-history-${channelId}`} onClick={() => setExpanded((value) => !value)} className="focus-visible:ring-ring flex w-full shrink-0 flex-wrap items-center gap-x-3 gap-y-1 rounded-xl px-4 py-3 text-left outline-none focus-visible:ring-2 focus-visible:ring-inset">
        <span className="flex items-center gap-2 font-semibold"><span className={cn("size-1.5 rounded-full bg-muted-foreground/50", active > 0 && "bg-primary", waiting > 0 && "bg-warning-foreground")} />Team activity</span>
        <span aria-live="polite" className={cn("text-muted-foreground text-xs", waiting > 0 && "text-warning-foreground")}>{summary}</span>
        <ChevronDownIcon className={cn("text-muted-foreground ml-auto size-4 transition-transform motion-reduce:transition-none", expanded && "rotate-180")} />
      </button>
      {shownTasks.length > 0 && <div className="flex flex-col gap-2 px-2 pb-2">
        {shownTasks.map((task) => {
          const step = taskStep(task);
          const agent = state.agents[task.agent_id];
          const Icon = step.icon;
          const label = task.status === "waiting" ? task.waiting_for === "input" ? "Needs input" : task.waiting_for === "approval" ? "Needs approval" : "Needs attention" : task.status === "running" ? task.cancel_requested ? "Stopping" : "Working" : task.status === "completed" ? "Done" : task.status === "queued" ? "Queued" : task.status === "failed" ? "Failed" : "Cancelled";
          return <div key={task.id} className="border-border bg-background flex flex-wrap items-center gap-2.5 rounded-xl border px-3 py-2.5">
            <Avatar name={agentName(task.agent_id)} kind="agent" role={agent?.role} avatar={agent?.avatar} runtime={agent?.runtime} size={30} working={task.status === "running"} />
            <div className="min-w-0 flex-1 basis-32">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1"><span className="font-medium">{agentName(task.agent_id)}</span><span className={cn("text-muted-foreground inline-flex items-center gap-1 text-[11px]", task.status === "waiting" && "text-warning-foreground", task.status === "completed" && "text-brand-soft-foreground", task.status === "failed" && "text-destructive")}><Icon className={cn("size-3", task.status === "running" && "motion-safe:animate-spin")} />{label}</span></div>
              <p className="text-muted-foreground mt-0.5 truncate text-xs" title={task.progress}>{task.progress || "Task received"}</p>
              {expanded && <p className="text-muted-foreground mt-1 text-[11px]">{[since(task.updated_at), usageFor(task)].filter(Boolean).join(" · ")}</p>}
            </div>
            <div className="ml-auto">{step.action}</div>
          </div>;
        })}
        {!expanded && current.length > shownTasks.length && <Button variant="link" size="inline" className="self-start px-2 py-1 text-xs" onClick={() => setExpanded(true)}>Show {current.length - shownTasks.length} more active {current.length - shownTasks.length === 1 ? "task" : "tasks"}</Button>}
      </div>}
      {error && <p role="alert" className="text-destructive px-4 pb-3">{error}</p>}
      <div id={`task-history-${channelId}`} hidden={!expanded}>
      {expanded && (
        <div className="flex flex-col gap-2 px-3 pb-3">
          {schedules.map((schedule) => {
            const step = scheduleStep(schedule);
            return <div key={schedule.id} className="flex flex-wrap items-center gap-2 rounded-xl border border-border p-3"><CalendarClockIcon className="text-muted-foreground size-4" /><div className="min-w-0 flex-1 basis-32"><p className="break-words">{step.verb}</p><p className="text-muted-foreground mt-1 text-xs">{step.chip}</p></div>{step.action}</div>;
          })}
          {tasks.length === 0 && schedules.length === 0 && <p className="text-muted-foreground px-1 pb-2">Mention a teammate in chat to start. Their progress and results will appear here.</p>}
          {activity ? (
            <Box
              title={activity.taskId
                ? <>Activity for <strong>{agentName(activity.agentId ?? 0)}</strong>'s task</>
                : <>Activity in this channel</>}
              onClose={() => setActivity(null)}
            >
              {state.channels[channelId]?.human_ids.includes(state.me.id)
                ? <ActivityLog channelId={channelId} taskId={activity.taskId} />
                : <p className="text-muted-foreground">Join the channel to see its activity.</p>}
            </Box>
          ) : (
            <Button variant="link" size="inline" className="ms-4 self-start text-[12.5px]" onClick={() => setActivity({})}>
              Channel activity
            </Button>
          )}
          {detail?.status === "failed" && (
            <Box title={<>Why <strong>{agentName(detail.agent_id)}</strong>'s task failed</>} onClose={() => setDetailId(null)}>
              <ToolError
                className="max-w-none"
                name={agentName(detail.agent_id)}
                target={detail.progress}
                message={detail.error ?? "The task failed."}
                attempt={detail.attempts}
                retrying={busy === detail.id}
                onRetry={() => void control(detail.id, "retry")}
              />
            </Box>
          )}
          {detail?.status === "completed" && (
            <Box title={<>Result from <strong>{agentName(detail.agent_id)}</strong></>} onClose={() => setDetailId(null)}>
              {results[detail.id] ? <TaskResult detail={results[detail.id]} /> : <p className="text-muted-foreground">Loading result…</p>}
              {detail.delivery_status === "unknown" && (
                <p className="text-muted-foreground">Answer saved here. Band delivery could not be confirmed.</p>
              )}
            </Box>
          )}
          {memory && (
            <Box
              title={<>Saved knowledge for <strong>{agentName(memory.agentId)}</strong> in this channel.</>}
              onClose={() => setMemory(null)}
            >
              {memory.rows.length === 0 ? (
                <p className="text-muted-foreground">No saved memories yet.</p>
              ) : (
                <MemoryChips
                  className="max-w-none"
                  chips={memory.rows.map((row) => {
                    const text = `${row.key}: ${row.content}`;
                    return { id: row.key, text: text.length > 90 ? `${text.slice(0, 89)}…` : text, change: "existing" };
                  })}
                  onForget={(key) => void forget(memory.agentId, key)}
                />
              )}
            </Box>
          )}
        </div>
      )}
      </div>
    </section>
  );
}
