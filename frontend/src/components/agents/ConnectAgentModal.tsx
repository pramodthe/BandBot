import { useCallback, useId, useState } from "react";
import { ArrowLeftIcon, CheckCircle2Icon, ChevronDownIcon, LoaderCircleIcon } from "lucide-react";
import { api } from "@/api";
import { isAdmin, type State } from "@/store";
import type { AgentConnection, Channel, ConnectedAgentRuntime } from "@/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import Field from "@/components/shared/Field";
import AgentConnectionPicker from "./AgentConnectionPicker";
import RuntimeIcon from "./RuntimeIcon";
import { runtimeLabels } from "./runtime-options";
import BandAgentKeyField from "./BandAgentKeyField";

const descriptions: Record<ConnectedAgentRuntime, string> = {
  codex: "Sign in with ChatGPT to run Codex in this workspace, or connect an OpenAI API key.",
  claude: "Connect Claude with an Anthropic API key. Claude subscription sign-in is not available for this integration.",
  cursor: "Connect your Cursor account with a user or service-account API key.",
  openclaw: "Connect a running OpenClaw Gateway and select its dedicated agent. A server admin must approve the Gateway first.",
};

function modelFor(runtime: ConnectedAgentRuntime, connection: AgentConnection | null): string {
  const models = connection?.models ?? [];
  if (connection?.auth_method === "codex_chatgpt") return models[0] ?? "";
  if (runtime === "claude") return models.find((model) => model.includes("sonnet")) ?? models.find((model) => model.startsWith("claude-")) ?? "";
  if (runtime === "codex") return models.find((model) => model.includes("codex")) ?? models.find((model) => model.startsWith("gpt-")) ?? models[0] ?? "";
  if (runtime === "cursor") return models.find((model) => model !== "auto-smart") ?? "";
  return models[0] ?? "";
}

export default function ConnectAgentModal({ state, channel, runtime, onBack, onClose }: {
  state: State; channel: Channel | null; runtime: ConnectedAgentRuntime; onBack: () => void; onClose: () => void;
}) {
  const id = useId();
  const label = runtimeLabels[runtime];
  const [connection, setConnection] = useState<AgentConnection | null>(null);
  const [model, setModel] = useState("");
  const [showOptions, setShowOptions] = useState(false);
  const [name, setName] = useState(() => {
    const names = new Set(Object.values(state.agents).map((agent) => agent.name.toLowerCase()));
    let next = label;
    for (let suffix = 2; names.has(next.toLowerCase()); suffix += 1) next = `${label} ${suffix}`;
    return next;
  });
  const targetChannel = channel && !channel.is_dm ? channel : null;
  const [joinChannel, setJoinChannel] = useState(!!targetChannel);
  const [busy, setBusy] = useState(false);
  const [bandKey, setBandKey] = useState("");
  const [register, setRegister] = useState(false);
  const usesPlatform = state.band.api === "agent" && state.band.configured;
  const canRegister = !usesPlatform && state.band.online && (isAdmin(state) || !!state.band.personal);
  const usesRegistration = register && canRegister;
  const needsBand = usesPlatform ? !state.band.online : !usesRegistration && !bandKey.trim();
  const [error, setError] = useState("");
  const chooseConnection = useCallback((next: AgentConnection | null) => {
    setConnection(next); setModel(modelFor(runtime, next)); setError("");
  }, [runtime]);
  const add = async () => {
    if (!connection || !model || busy || needsBand) return;
    setBusy(true); setError("");
    try {
      await api("POST", "/api/agents", { runtime, connection_id: connection.id, name: name.trim(), model,
        band_agent_api_key: usesPlatform || usesRegistration ? undefined : bandKey.trim(),
        role: "custom", instructions: "", specialty: "", plugins: [],
        channel_id: joinChannel ? targetChannel?.id ?? null : null });
      onClose();
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  return <Dialog open onOpenChange={(open) => !open && !busy && onClose()}>
    <DialogContent showCloseButton={!busy} className="flex max-h-[calc(100dvh-32px)] flex-col gap-0 overflow-hidden rounded-2xl p-0 sm:max-w-[480px]"
      onEscapeKeyDown={(event) => busy && event.preventDefault()} onPointerDownOutside={(event) => busy && event.preventDefault()}>
      <DialogHeader className="shrink-0 px-6 pt-5 pr-12 pb-4 text-left">
        <Button type="button" variant="ghost" size="sm" className="-ml-2 mb-2 self-start" disabled={busy} onClick={onBack}><ArrowLeftIcon />All agents</Button>
        <DialogTitle className="flex items-center gap-3 text-xl"><RuntimeIcon runtime={runtime} className="size-7" />Connect {label}</DialogTitle>
        <DialogDescription className="mt-2 leading-relaxed">{descriptions[runtime]}</DialogDescription>
      </DialogHeader>
      <div className="min-h-0 space-y-4 overflow-y-auto px-6 pb-5">
        <AgentConnectionPicker provider={runtime} value={connection} onChange={chooseConnection} disabled={busy} />
        {connection && <>
          <div role="status" className="bg-muted/50 space-y-2 rounded-xl p-4">
            <p className="flex items-center gap-2 text-sm font-medium"><CheckCircle2Icon className="size-4" />Account authenticated</p>
            <p className="text-muted-foreground text-xs leading-relaxed">{connection.label}. {model ? `Selected model: ${model}.` : "Choose a model in Connection options."} Add {label} to start using it here.</p>
          </div>
          {targetChannel && <label className="flex items-center gap-2.5 text-sm"><Checkbox checked={joinChannel} disabled={busy} onCheckedChange={(checked) => setJoinChannel(checked === true)} />Add to #{targetChannel.name}</label>}
          <Button variant="ghost" className="w-full justify-between px-0" disabled={busy} aria-expanded={showOptions} aria-controls={`${id}-options`} onClick={() => setShowOptions(!showOptions)}>Connection options<ChevronDownIcon className={`size-4 transition-transform ${showOptions ? "rotate-180" : ""}`} /></Button>
          {showOptions && <div id={`${id}-options`} className="space-y-3">
            <Field id={`${id}-name`} label="Display name"><Input id={`${id}-name`} value={name} minLength={3} maxLength={40} disabled={busy} onChange={(event) => setName(event.target.value)} /></Field>
            <Field id={`${id}-model`} label="Model"><Select value={model} onValueChange={setModel} disabled={busy}>
              <SelectTrigger id={`${id}-model`} className="w-full"><SelectValue placeholder="Choose a model" /></SelectTrigger>
              <SelectContent>{connection.models.filter((item) => runtime !== "cursor" || item !== "auto-smart").map((item) => <SelectItem key={item} value={item}>{item}</SelectItem>)}</SelectContent>
            </Select></Field>
          </div>}
        </>}
        <p className="text-muted-foreground text-xs leading-relaxed">Conversations with this agent appear in this workspace. Existing chats in your provider's app stay separate.</p>
        {usesPlatform ? <p role="status" className="bg-sidebar rounded-xl border p-4 text-sm">{state.band.online
          ? "Using your saved Band connection. No additional Band key is needed."
          : "Reconnect your Band key in People before adding this teammate."}</p>
          : <BandAgentKeyField value={bandKey} onChange={(key) => { setBandKey(key); setError(""); }} disabled={busy}
            canRegister={canRegister} register={usesRegistration} onRegisterChange={setRegister} />}
        {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
      </div>
      <div className="border-border flex shrink-0 justify-end gap-2 border-t px-6 py-4">
        <Button variant="ghost" disabled={busy} onClick={onClose}>Cancel</Button>
        <Button disabled={busy || !connection || !model || name.trim().length < 3 || needsBand} onClick={() => void add()}>
          {busy && <LoaderCircleIcon className="size-4 animate-spin" />}{busy ? "Adding…" : `Add ${label}`}
        </Button>
      </div>
    </DialogContent>
  </Dialog>;
}
