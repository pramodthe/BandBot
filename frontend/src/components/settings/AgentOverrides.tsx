import { api } from "@/api";
import type { Agent, ServiceSelection } from "@/types";
import { Button } from "@/components/ui/button";
import ServiceSelect from "./ServiceSelect";
import { selectClass, type Perform, type ServiceChoice } from "./provider-settings";

/** Per-agent reasoning model override for new tasks. */
export default function AgentOverrides({ agents, agentId, selection, choices, busy, perform, onAgent, onSelection }: {
  agents: Agent[];
  agentId: string;
  selection: ServiceSelection;
  choices: ServiceChoice[];
  busy: boolean;
  perform: Perform;
  onAgent: (agentId: string) => void;
  onSelection: (selection: ServiceSelection) => void;
}) {
  const save = () => void perform(
    () => api<ServiceSelection>("PUT", `/api/settings/agents/${agentId}/services`, { expected_version: selection.version, bindings: selection.bindings })
      .then(onSelection),
    "Agent selection saved for new tasks.",
  );
  return (
    <section className="flex flex-col gap-2" aria-label="Agent model overrides">
      <h3 className="font-semibold">Agent model overrides</h3>
      <select aria-label="Agent" className={selectClass} value={agentId} onChange={(e) => onAgent(e.target.value)}>
        <option value="">Choose an agent…</option>
        {agents.filter((a) => a.runtime === "deepagents").map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
      </select>
      {agentId && <>
        <ServiceSelect
          service="llm"
          value={selection.bindings.llm}
          choices={choices}
          onChange={(binding) => onSelection({ ...selection, bindings: binding.disabled ? {} : { llm: binding } })}
          inherit
        />
        <Button variant="outline" disabled={busy} onClick={save}>Save agent override</Button>
      </>}
    </section>
  );
}
