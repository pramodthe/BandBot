import { useState } from "react";
import { ChevronDownIcon, ChevronRightIcon, HistoryIcon, MonitorIcon } from "lucide-react";
import type { Agent, ChannelComputer } from "@/types";
import { cn, formatClock } from "@/lib/utils";
import Avatar from "@/components/shared/Avatar";
import { ComputerUse } from "@/components/assistant-ui/elements/computer-use";
import { Button } from "@/components/ui/button";
import { useComputerState } from "./useComputerState";
import type { OpenComputer } from "@/components/workspace/types";
import RegisterAppPreview from "./RegisterAppPreview";

export default function ComputerActivity({ agent, activity, openComputer }: {
  agent: Agent;
  activity: ChannelComputer;
  openComputer: OpenComputer;
}) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const showDetails = activity.computer_working || detailsOpen;
  const { computer, error } = useComputerState(activity.task_id, activity.computer_working, showDetails);
  const steps = computer?.steps ?? [];
  const viewing = computer?.control;
  const status = viewing ? `${viewing.user_name} has control`
    : activity.computer_working ? "Task using computer" : "Last computer activity";

  if (!showDetails) return <div className="mx-auto flex w-full max-w-[920px] flex-wrap items-center gap-x-3 px-6 py-2 pl-[4.25rem] max-sm:px-4 max-sm:pl-14">
    <Button variant="ghost" size="sm" className="text-muted-foreground" aria-expanded={false} onClick={() => setDetailsOpen(true)}><ChevronRightIcon /><HistoryIcon />Computer activity</Button>
    <Button variant="link" size="inline" className="text-muted-foreground text-xs" onClick={() => openComputer(agent.id, activity.task_id)}>Open computer</Button>
  </div>;

  return (
    <div className="mx-auto flex w-full max-w-[920px] gap-3 px-6 py-3 max-sm:px-4" aria-label={`${agent.name}'s computer activity`}>
      <Avatar name={agent.name} kind="agent" role={agent.role} avatar={agent.avatar} runtime={agent.runtime} />
      <div className="min-w-0 flex-1">
        <div className="mb-2 flex max-w-md flex-wrap items-center gap-x-2 gap-y-1 text-xs">
          <span className="text-sm font-semibold">{agent.name}'s computer</span>
          <span className="text-muted-foreground flex items-center gap-1.5" role="status">
            <span className={cn("size-1.5 rounded-full bg-muted-foreground", activity.computer_working && !viewing && "animate-pulse bg-primary motion-reduce:animate-none")} />
            {status}
          </span>
          {!activity.computer_working ? <Button variant="ghost" size="sm" className="ml-auto" aria-label="Hide computer activity" aria-expanded={true} onClick={() => setDetailsOpen(false)}><ChevronDownIcon /></Button> : null}
        </div>
        <ComputerUse url={computer?.url || "Cloud desktop"} steps={steps} activeIndex={steps.length - 1}>
          {computer?.image ? <img src={computer.image} alt={`${agent.name}'s latest computer observation`} className="block h-auto w-full" />
            : <div className="text-muted-foreground flex min-h-36 items-center justify-center gap-2 p-4 text-center text-xs">
              <MonitorIcon className="size-4 shrink-0" />{error || "Waiting for the agent's first screenshot…"}
            </div>}
        </ComputerUse>
        {computer?.network_warning && <p role="alert" className="mt-2 max-w-md rounded-lg border border-border bg-muted p-3 text-xs">{computer.network_warning}</p>}
        <div className="mt-1 flex max-w-md flex-wrap items-center justify-between gap-x-2 text-xs">
          <span className="text-muted-foreground">{computer?.observed_at
            ? `Observed ${formatClock(computer.observed_at, { seconds: true })}`
            : "Computer observations"}</span>
          <div className="flex flex-wrap items-center gap-3"><RegisterAppPreview taskId={activity.task_id} />
          <Button variant="link" size="inline" onClick={() => openComputer(agent.id, activity.task_id)}>
            <MonitorIcon data-icon="inline-start" />Open computer
          </Button></div>
        </div>
      </div>
    </div>
  );
}
