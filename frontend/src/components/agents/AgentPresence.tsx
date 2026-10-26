import type { State } from "@/store";
import type { Agent, TaskActivity } from "@/types";
import StatusDot from "@/components/shared/StatusDot";

const PRIORITY = { waiting: 3, running: 2, queued: 1, done: 0 };

export function getAgentActivity(state: State, agentId: number): TaskActivity | undefined {
  let active: TaskActivity | undefined;
  for (const channel of Object.values(state.activity)) {
    for (const task of Object.values(channel)) {
      if (task.agent_id === agentId && task.status !== "done" && (!active || PRIORITY[task.status] > PRIORITY[active.status])) active = task;
    }
  }
  return active;
}

export default function AgentPresence({ agent, activity }: { agent: Agent; activity?: TaskActivity }) {
  const label = activity?.status === "waiting" ? "Needs attention" : activity?.status === "running" ? "Working" : activity?.status === "queued" ? "Queued" : { online: "Online", starting: "Starting", stopped: "Offline", error: "Needs attention" }[agent.status.state];
  const status = activity?.status === "waiting" || activity?.status === "queued" ? "starting" : activity?.status === "running" ? "online" : agent.status.state;
  return <span className="text-muted-foreground inline-flex shrink-0 items-center gap-1.5 text-xs"><StatusDot state={status} />{label}</span>;
}
