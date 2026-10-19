import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { api } from "@/api";
import type { AuditEvent } from "@/types";
import { cn, parseUtc } from "@/lib/utils";
import { Button } from "@/components/ui/button";

const PAGE = 200;

const text = (value: unknown) => (typeof value === "string" ? value : value == null ? "" : JSON.stringify(value));

function describe(event: AuditEvent): string {
  const detail = event.detail;
  const field = (key: string) => text(detail[key]);
  switch (event.action) {
    case "tool_call": {
      const args = Object.entries((detail.args ?? {}) as Record<string, unknown>)
        .slice(0, 2)
        .map(([name, value]) => `${name}: ${text(value)}`)
        .join(", ");
      return `used ${field("tool")}${detail.via === "helper" ? " through a helper" : ""}${args ? ` (${args})` : ""}`;
    }
    case "approval_requested":
      return `asked for approval: ${field("action")}`;
    case "approval_approved":
    case "approval_denied":
      return `${event.action === "approval_approved" ? "approved" : "denied"}: ${field("action")}${detail.note ? ` (${field("note")})` : ""}`;
    case "handoff":
      return `handed off to ${field("teammate")}${detail.outside ? " (outside Band agent)" : ""}: ${field("request")}`;
    case "schedule_created":
      return `scheduled ${field("schedule")}: ${field("prompt")}`;
    case "schedule_cancelled":
      return `cancelled a schedule (${field("schedule")}): ${field("prompt")}`;
    case "computer_taken_over":
      return `took over ${field("agent")}'s computer`;
    case "computer_handed_back":
      return `handed ${field("agent")}'s computer back`;
    case "computer_control_expired":
      return `ended ${field("user")}'s take-over of ${field("agent")}'s computer after its time ran out`;
    case "computer_upload":
      return `uploaded ${field("path")} to the computer`;
    case "computer_download":
      return `downloaded ${field("path")} from the computer`;
    case "computer_stopped":
      return `stopped ${field("agent")}'s computer`;
    case "agent_created":
      return `created ${field("agent")} (${field("role")}, ${field("model")})`;
    case "agent_updated":
      return `changed ${field("agent")}'s ${Object.keys(detail).filter((key) => key !== "agent").join(" and ")}`;
    case "agent_deleted":
      return `deleted ${field("agent")}`;
    case "agent_added":
      return `added ${field("agent")} to the channel`;
    case "agent_removed":
      return `removed ${field("agent")} from the channel`;
    case "outside_agent_invited":
      return "invited an outside Band agent";
    case "outside_agent_removed":
      return "removed an outside Band agent";
    case "task_cancelled":
      return detail.approvals_denied ? `cancelled a task and denied its ${field("approvals_denied")} approval request(s)` : "cancelled a task";
    case "task_retried":
      return "resumed a task";
    case "task_stopped":
      return `stopped a task: ${field("reason")}`;
    case "memory_forgotten":
      return `deleted the memory "${field("key")}"`;
    case "policy_changed":
      return `set ${Object.entries(detail)
        .map(([key, value]) => `${key === "approvers" ? "approvals" : "live computer access"} to ${value === "admins" ? "admins only" : "any member"}`)
        .join(" and ")}`;
    case "budget_changed":
      return `set ${field("target")}'s daily budget to ${
        typeof detail.daily_tokens === "number" ? `${detail.daily_tokens.toLocaleString()} tokens` : "no limit"
      }`;
    case "role_changed":
      return `made ${field("user")} ${field("role") === "admin" ? "an admin" : "a member"}`;
    default:
      return event.action.replaceAll("_", " ");
  }
}

function when(iso: string) {
  const at = parseUtc(iso);
  const clock = at.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return at.toDateString() === new Date().toDateString()
    ? clock
    : `${at.toLocaleDateString([], { month: "short", day: "numeric" })} ${clock}`;
}

/** What people and agents did in a channel (or one task), oldest first. */
export default function ActivityLog({ channelId, taskId }: { channelId: number; taskId?: string }) {
  const [events, setEvents] = useState<AuditEvent[] | null>(null);
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState("");
  const list = useRef<HTMLOListElement>(null);
  const pinned = useRef(true);
  const query = taskId ? `task_id=${encodeURIComponent(taskId)}` : "";

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    let newest: number | null = null;
    setEvents(null);
    setComplete(false);
    pinned.current = true;
    const poll = async () => {
      try {
        if (newest === null) {
          const rows = (await api<AuditEvent[]>("GET", `/api/channels/${channelId}/audit?${query}`)).reverse();
          if (stopped) return;
          newest = rows.at(-1)?.id ?? 0;
          setEvents(rows);
          setComplete(rows.length < PAGE);
        } else {
          // Newer events come oldest first; keep reading until a short page so none are skipped.
          let rows: AuditEvent[];
          do {
            rows = await api<AuditEvent[]>("GET", `/api/channels/${channelId}/audit?${query}&after=${newest}`);
            if (stopped) return;
            if (rows.length) {
              newest = rows[rows.length - 1].id;
              const added = rows;
              setEvents((current) => [...(current ?? []), ...added]);
            }
          } while (rows.length === PAGE);
        }
        setError("");
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 3000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId, query]);

  useLayoutEffect(() => {
    if (pinned.current && list.current) list.current.scrollTop = list.current.scrollHeight;
  }, [events]);

  const loadEarlier = async () => {
    const oldest = events?.[0];
    if (!oldest) return;
    try {
      const rows = await api<AuditEvent[]>("GET", `/api/channels/${channelId}/audit?${query}&before=${oldest.id}`);
      pinned.current = false;
      setEvents((current) => [...rows.reverse(), ...(current ?? [])]);
      if (rows.length < PAGE) setComplete(true);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  if (error && events === null) return <p role="alert" className="text-destructive">{error}</p>;
  if (events === null) return <p className="text-muted-foreground">Loading activity…</p>;
  if (events.length === 0) return <p className="text-muted-foreground">Nothing has happened here yet.</p>;
  return (
    <div className="flex flex-col gap-1.5">
      {!complete && (
        <Button variant="link" size="inline" className="self-start text-[12.5px]" onClick={() => void loadEarlier()}>
          Show earlier activity
        </Button>
      )}
      <ol
        ref={list}
        className="flex max-h-64 flex-col gap-1 overflow-y-auto"
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 8;
        }}
      >
        {events.map((event) => (
          <li key={event.id} className="flex gap-2 leading-snug">
            <time className="text-muted-foreground w-[4.5rem] shrink-0 text-xs tabular-nums" dateTime={event.at}>
              {when(event.at)}
            </time>
            <span className={cn("min-w-0 break-words", event.actor_type === "system" && "text-muted-foreground italic")}>
              <strong>{event.actor_name}</strong> {describe(event)}
            </span>
          </li>
        ))}
      </ol>
      {error && <p role="alert" className="text-destructive text-xs">{error}</p>}
    </div>
  );
}
