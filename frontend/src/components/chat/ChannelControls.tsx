import { useEffect, useState } from "react";
import { api } from "@/api";
import { isAdmin, type State } from "@/store";
import type { Channel, ChannelPolicy, ChannelUsage } from "@/types";
import { cn, formatCost, formatTokens } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

const hint = "text-muted-foreground text-[12.5px]";
const WHO = { members: "Any member", admins: "Admins only" } as const;

function PolicyRow({ label, value, editable, onChange }: {
  label: string; value: ChannelPolicy["approvers"]; editable: boolean; onChange: (value: ChannelPolicy["approvers"]) => void;
}) {
  return (
    <div className="flex items-center justify-between gap-2 text-[13px]">
      <span>{label}</span>
      {editable ? (
        <Select value={value} onValueChange={(next) => onChange(next as ChannelPolicy["approvers"])}>
          <SelectTrigger size="sm" className="h-7 w-[118px] bg-white text-[12.5px] shadow-none" aria-label={label}>
            <SelectValue />
          </SelectTrigger>
          <SelectContent position="popper">
            <SelectItem value="members">{WHO.members}</SelectItem>
            <SelectItem value="admins">{WHO.admins}</SelectItem>
          </SelectContent>
        </Select>
      ) : (
        <span className={hint}>{WHO[value]}</span>
      )}
    </div>
  );
}

function Meter({ used, limit }: { used: number; limit: number | null }) {
  if (!limit) return null;
  const share = Math.min(1, used / limit);
  return (
    <div className="h-1.5 overflow-hidden rounded-full bg-[#eee]" role="meter" aria-valuenow={used} aria-valuemax={limit}>
      <div className={cn("h-full rounded-full", share >= 1 ? "bg-destructive" : share > 0.8 ? "bg-[#d9a400]" : "bg-online")}
        style={{ width: `${share * 100}%` }} />
    </div>
  );
}

function BudgetInput({ label, value, onSave }: { label: string; value: number | null; onSave: (tokens: number | null) => void }) {
  const [draft, setDraft] = useState(value ? String(value) : "");
  useEffect(() => setDraft(value ? String(value) : ""), [value]);
  const parsed = draft.trim() === "" ? null : Math.round(Number(draft));
  const changed = parsed !== value && (parsed === null || Number.isFinite(parsed));
  return (
    <form className="flex items-center gap-1.5" onSubmit={(e) => { e.preventDefault(); if (changed) onSave(parsed); }}>
      <Input
        type="number"
        min={1000}
        step={1000}
        inputMode="numeric"
        placeholder="No limit"
        aria-label={label}
        className="h-7 bg-white text-[12.5px]"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
      />
      <Button type="submit" variant="chip" size="chip" disabled={!changed}>Save</Button>
    </form>
  );
}

/** Who may act for this channel's agents, and what they spent today against their daily token budgets. */
export default function ChannelControls({ state, channel }: { state: State; channel: Channel }) {
  const admin = isAdmin(state);
  const [usage, setUsage] = useState<ChannelUsage | null>(null);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const next = await api<ChannelUsage>("GET", `/api/channels/${channel.id}/usage`);
        if (!stopped) setUsage(next);
      } catch (err) {
        if (!stopped) setError((err as Error).message);
      } finally {
        if (!stopped) timer = window.setTimeout(poll, 10000);
      }
    };
    void poll();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channel.id, state.budgetRevision]);

  const run = (p: Promise<unknown>) => p.then(() => setError("")).catch((e) => setError((e as Error).message));
  const setPolicy = (patch: Partial<ChannelPolicy>) => run(api("PUT", `/api/channels/${channel.id}/policy`, patch));
  const setBudget = (scope: "agent" | "channel", scopeId: number, tokens: number | null) =>
    run(api("PUT", "/api/budgets", { scope, scope_id: scopeId, daily_tokens: tokens }));
  const cost = (usd: number | null) => (usd != null ? ` · ${formatCost(usd)}` : "");

  return (
    <div className="flex flex-col gap-2">
      <PolicyRow label="Approve agent actions" value={channel.policy.approvers} editable={admin}
        onChange={(approvers) => void setPolicy({ approvers })} />
      <PolicyRow label="Watch or take over computers" value={channel.policy.computer_control} editable={admin}
        onChange={(computer_control) => void setPolicy({ computer_control })} />

      {usage && (
        <div className="mt-1 flex flex-col gap-1.5">
          <div className="flex items-baseline justify-between gap-2 text-[13px]">
            <span>Tokens today</span>
            <span className={hint}>
              {formatTokens(usage.tokens)}{usage.budget ? ` of ${formatTokens(usage.budget)}` : ""}{cost(usage.cost)}
            </span>
          </div>
          <Meter used={usage.tokens} limit={usage.budget} />
          {editing && <BudgetInput label="Channel daily token budget" value={usage.budget}
            onSave={(tokens) => void setBudget("channel", channel.id, tokens)} />}
          {usage.agents.map((row) => {
            const agent = state.agents[row.agent_id];
            if (!agent) return null;
            return (
              <div key={row.agent_id} className="flex flex-col gap-1">
                <div className="flex items-baseline justify-between gap-2 text-[12.5px]">
                  <span className="truncate">{agent.name}</span>
                  <span className={hint} title="Here today; in brackets, all channels against the agent's own budget">
                    {formatTokens(row.tokens)}{cost(row.cost)}
                    {row.agent_budget ? ` (${formatTokens(row.agent_tokens_today)} of ${formatTokens(row.agent_budget)})` : ""}
                  </span>
                </div>
                {editing && <BudgetInput label={`${agent.name} daily token budget, all channels`} value={row.agent_budget}
                  onSave={(tokens) => void setBudget("agent", row.agent_id, tokens)} />}
              </div>
            );
          })}
          <div className={hint}>
            Budgets reset at midnight UTC. A task over budget waits, and a running one stops until it is raised.
          </div>
          {admin && (
            <Button variant="link" size="inline" className="self-start text-[12.5px]" onClick={() => setEditing((v) => !v)}>
              {editing ? "Done editing budgets" : "Edit daily budgets"}
            </Button>
          )}
        </div>
      )}
      {error && <div className="text-destructive text-[12.5px]">{error}</div>}
    </div>
  );
}
