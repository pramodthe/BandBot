import { useId, useState, type FormEvent } from "react";
import { ArrowLeftIcon, ChevronDownIcon, LoaderCircleIcon } from "lucide-react";
import { api } from "@/api";
import type { State } from "@/store";
import type { Channel } from "@/types";
import RuntimeIcon from "./RuntimeIcon";
import AgentAvatarPicker from "./AgentAvatarPicker";
import { agentAvatar, AGENT_AVATARS } from "@/components/shared/Avatar";
import Field from "@/components/shared/Field";
import PluginChoices from "@/components/settings/PluginChoices";
import BandAgentKeyField from "./BandAgentKeyField";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export default function CreateAgentModal({ state, channel, onClose, onBack }: {
  state: State;
  channel: Channel | null;
  onClose: () => void;
  onBack?: () => void;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [specialty, setSpecialty] = useState("");
  const [avatar, setAvatar] = useState<string>(() => {
    const counts = new Map(AGENT_AVATARS.map((choice) => [choice, 0]));
    for (const agent of Object.values(state.agents)) {
      const choice = agentAvatar(agent.avatar, agent.role);
      counts.set(choice, (counts.get(choice) ?? 0) + 1);
    }
    return [...AGENT_AVATARS].sort((a, b) => counts.get(a)! - counts.get(b)!)[0];
  });
  const defaultModel = state.models.find((m) => m.default)?.id ?? state.models[0]?.id ?? "";
  const [model, setModel] = useState(defaultModel);
  const [instructions, setInstructions] = useState("");
  const [addToChannel, setAddToChannel] = useState(false);
  const [plugins, setPlugins] = useState<string[] | null>([]);
  const [advanced, setAdvanced] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [bandKey, setBandKey] = useState("");
  const canManage = !!state.band.can_manage_agents;
  const usesPlatform = canManage || (state.band.api === "agent" && state.band.configured);
  const needsBand = usesPlatform ? !canManage && !state.band.online : !bandKey.trim();
  const targetChannel = channel && !channel.is_dm ? channel : null;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy || needsBand || !model || name.trim().length < 3) return;
    setBusy(true);
    setError("");
    try {
      await api("POST", "/api/agents", {
        name: name.trim(), role: "custom", specialty, avatar, model, instructions,
        band_agent_api_key: usesPlatform ? undefined : bandKey.trim(),
        channel_id: addToChannel && targetChannel ? targetChannel.id : null,
        plugins,
      });
      onClose();
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
      <DialogContent showCloseButton={!busy} className="flex max-h-[calc(100dvh-32px)] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-[512px]"
        onEscapeKeyDown={(e) => busy && e.preventDefault()} onPointerDownOutside={(e) => busy && e.preventDefault()}>
        <DialogHeader className="shrink-0 px-6 pt-6 pr-12 pb-2 text-left">
          {onBack && <Button type="button" variant="ghost" size="sm" className="-ml-2 mb-2 self-start" disabled={busy} onClick={onBack}><ArrowLeftIcon />All agents</Button>}
          <DialogTitle className="flex items-center gap-2 text-lg"><RuntimeIcon runtime="deepagents" />{usesPlatform ? "Create agent" : "Connect Band agent"}</DialogTitle>
          <DialogDescription className="mt-1 leading-relaxed">
            {canManage ? "Create an agent in your Band account and choose how it works here." : usesPlatform ? "Give your teammate a name, appearance, and instructions. It uses your saved Band connection." : "Connect your Band agent, choose its appearance, and set how it works here."}
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex min-h-0 flex-col">
          <div className="min-h-0 space-y-5 overflow-y-auto px-6 pt-1 pb-6">
            <AgentAvatarPicker name={name} avatar={avatar} onChange={setAvatar} disabled={busy} />
            <Field id={`${id}-name`} label="Name">
              <Input id={`${id}-name`} autoFocus required minLength={3} maxLength={40} autoComplete="off"
                disabled={busy} value={name} onChange={(e) => { setName(e.target.value); setError(""); }} placeholder="e.g. Atlas" />
            </Field>
            <Field id={`${id}-specialty`} label="Specialty" hint="(optional)">
              <Input id={`${id}-specialty`} maxLength={160} disabled={busy} value={specialty}
                aria-describedby={`${id}-specialty-hint`} onChange={(e) => setSpecialty(e.target.value)} placeholder="e.g. Research and synthesis" />
              <p id={`${id}-specialty-hint`} className="text-muted-foreground text-xs leading-relaxed">
                Tell teammates what this agent is best at. This also guides how it works.
              </p>
            </Field>
            <div className="border-border border-y py-1">
              <Button type="button" variant="ghost" className="w-full justify-between px-0 hover:bg-transparent"
                aria-expanded={advanced} aria-controls={`${id}-settings`} onClick={() => setAdvanced(!advanced)}>
                More settings <ChevronDownIcon className={`size-4 transition-transform ${advanced ? "rotate-180" : ""}`} />
              </Button>
              {advanced && <div id={`${id}-settings`} className="space-y-5 pt-3 pb-4">
                <Field id={`${id}-model`} label="Model">
                  <Select value={model} onValueChange={setModel} disabled={busy}>
                    <SelectTrigger id={`${id}-model`} className="w-full"><SelectValue placeholder="Choose a model" /></SelectTrigger>
                    <SelectContent position="popper"><SelectGroup>
                      {state.models.map((m) => <SelectItem key={m.id} value={m.id}>{m.label}</SelectItem>)}
                    </SelectGroup></SelectContent>
                  </Select>
                </Field>
                <Field id={`${id}-instructions`} label="Instructions" hint="(optional)">
                  <Textarea id={`${id}-instructions`} rows={4} maxLength={6000} disabled={busy} value={instructions}
                    onChange={(e) => setInstructions(e.target.value)} placeholder="How should this agent approach its work?" />
                </Field>
                <fieldset disabled={busy}><PluginChoices value={plugins} onChange={setPlugins} /></fieldset>
              </div>}
            </div>
            {targetChannel && <label className="flex items-center gap-2.5 text-sm">
              <Checkbox checked={addToChannel} disabled={busy} onCheckedChange={(checked) => setAddToChannel(checked === true)} />
              <span className="min-w-0 break-words">Add to #{targetChannel.name}</span>
            </label>}
            <p className="text-muted-foreground text-xs leading-relaxed">
              {addToChannel && targetChannel ? `Joins #${targetChannel.name} after creation.` : "You can add it later from a channel’s Members panel."}
              {plugins !== null && plugins.length === 0 ? " Connected apps are off until you choose them in More settings." : " Connected apps follow your selection in More settings."}
            </p>
            {usesPlatform ? <p role="status" className="bg-sidebar rounded-xl border p-4 text-sm">{canManage
              ? "Using your own Band user key to create the agent. Its agent key will be saved securely for runtime access."
              : state.band.online ? "Using your saved Band connection. No additional Band key is needed."
              : "Your Band connection is unavailable. Update or reconnect your key in People before creating a teammate."}</p>
              : <BandAgentKeyField value={bandKey} onChange={(key) => { setBandKey(key); setError(""); }} disabled={busy} />}
            {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
          </div>
          <div className="border-border flex shrink-0 justify-end gap-2 border-t px-6 py-4">
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={busy || needsBand || name.trim().length < 3 || !model}>
              {busy && <LoaderCircleIcon className="size-4 animate-spin" />}{busy ? usesPlatform ? "Creating…" : "Connecting…" : usesPlatform ? "Create agent" : "Connect agent"}
            </Button>
          </div>
        </form>
      </DialogContent>
    </Dialog>
  );
}
