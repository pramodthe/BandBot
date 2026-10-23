import { useId, useState, type FormEvent } from "react";
import { LoaderCircleIcon } from "lucide-react";
import { api } from "@/api";
import { isAdmin, type State } from "@/store";
import type { Agent } from "@/types";
import AgentAvatarPicker from "./AgentAvatarPicker";
import Field from "@/components/shared/Field";
import ModalShell, { actionsClass, formClass } from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function AgentModal({ state, agent, onClose, onUpdated }: {
  state: State;
  agent: Agent;
  onClose: () => void;
  onUpdated: (agent: Agent) => void;
}) {
  const id = useId();
  const [original] = useState(agent);
  const [role, setRole] = useState(agent.role);
  const [model, setModel] = useState(agent.model);
  const [specialty, setSpecialty] = useState(agent.specialty ?? "");
  const [avatar, setAvatar] = useState(agent.avatar ?? null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const dirty = role !== original.role || model !== original.model || specialty !== (original.specialty ?? "") || avatar !== (original.avatar ?? null);
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !dirty) return;
    setBusy(true); setError("");
    try {
      const updated = await api<Agent>("PATCH", `/api/agents/${agent.id}`, {
        ...(role !== original.role && !agent.is_master ? { role } : {}),
        ...(model !== original.model ? { model } : {}),
        ...(specialty !== (original.specialty ?? "") ? { specialty } : {}),
        ...(avatar !== (original.avatar ?? null) ? { avatar } : {}),
      });
      onUpdated(updated);
      onClose();
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };

  return (
    <ModalShell title={`Edit ${agent.name} profile`} onClose={() => !busy && onClose()}>
      <form className={formClass} onSubmit={submit}>
        <AgentAvatarPicker name={agent.name} avatar={avatar} role={role} onChange={setAvatar} disabled={busy} />
        <Field id={`${id}-specialty`} label="Specialty" hint="(optional)">
          <Input id={`${id}-specialty`} value={specialty} maxLength={160} disabled={busy}
            placeholder="e.g. Research and synthesis" onChange={(event) => setSpecialty(event.target.value)} />
          <p className="text-muted-foreground text-xs">Helps teammates understand this agent’s focus and guides how it works.</p>
        </Field>
        <Field id={`${id}-model`} label="Intelligence">
          {(agent.model_ref || (agent.runtime && agent.runtime !== "deepagents")) ? <Input id={`${id}-model`} value={model} maxLength={120} disabled={busy || !(isAdmin(state) || agent.owner_id === state.me.id)} onChange={(event) => setModel(event.target.value)} /> : <Select value={model} onValueChange={setModel} disabled={busy || !(isAdmin(state) || agent.owner_id === state.me.id)}>
            <SelectTrigger id={`${id}-model`} className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent position="popper"><SelectGroup>{state.models.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>}
          <p className="text-muted-foreground text-xs">{(isAdmin(state) || agent.owner_id === state.me.id) ? "Model changes apply to new tasks. Admins select connections in Providers & APIs." : "Only workspace admins can change the model."}</p>
        </Field>
        {!agent.is_master ? <Field id={`${id}-role`} label="Base role">
          <Select value={role} onValueChange={setRole} disabled={busy}>
            <SelectTrigger id={`${id}-role`} className="w-full"><SelectValue /></SelectTrigger>
            <SelectContent position="popper"><SelectGroup>{state.roles.map((item) => <SelectItem key={item.id} value={item.id}>{item.label}</SelectItem>)}</SelectGroup></SelectContent>
          </Select>
          <p className="text-muted-foreground text-xs">Provides built-in guidance. Add your own instructions on the profile.</p>
        </Field> : null}
        {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
        <div className={actionsClass}>
          <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
          <Button type="submit" disabled={busy || !dirty}>{busy ? <LoaderCircleIcon className="animate-spin" data-icon="inline-start" /> : null}Save profile</Button>
        </div>
      </form>
    </ModalShell>
  );
}
