import { useEffect, useState, type FormEvent } from "react";
import { ChevronDownIcon } from "lucide-react";
import { api } from "@/api";
import Field from "@/components/shared/Field";
import ModalShell, { actionsClass, formClass } from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { DropdownMenu, DropdownMenuCheckboxItem, DropdownMenuContent, DropdownMenuLabel, DropdownMenuTrigger } from "@/components/ui/dropdown-menu";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Alert, AlertDescription } from "@/components/ui/alert";

interface Settings { configured: boolean; models: string[]; default_model: string; suggested_models: { id: string; label: string }[] }

export default function OpenAISettingsModal({ onClose }: { onClose: () => void }) {
  const [loaded, setLoaded] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [models, setModels] = useState<string[]>([]);
  const [defaultModel, setDefaultModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let stopped = false;
    api<Settings>("GET", "/api/settings/openai").then((value) => {
      if (!stopped) { setLoaded(value); setModels(value.models); setDefaultModel(value.default_model); }
    }).catch((e: Error) => { if (!stopped) setError(e.message); });
    return () => { stopped = true; };
  }, []);
  const options = [...new Map([
    ...(loaded?.models ?? []).map((id) => [id, { id, label: id }] as const),
    ...(loaded?.suggested_models ?? []).map((model) => [model.id, model] as const),
  ]).values()];
  const labelFor = (id: string) => options.find((model) => model.id === id)?.label ?? id;
  const toggleModel = (id: string, checked: boolean) => {
    const next = checked ? [...new Set([...models, id])] : models.filter((model) => model !== id);
    if (next.length === 0 || next.length > 12) return;
    setModels(next);
    if (!next.includes(defaultModel)) setDefaultModel(next[0]);
    setSaved(false);
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError(""); setSaved(false);
    try {
      const result = await api<Settings>("PUT", "/api/settings/openai", {
        api_key: key || null, models, default_model: defaultModel,
      });
      setLoaded(result); setModels(result.models); setDefaultModel(result.default_model); setKey(""); setSaved(true);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <ModalShell title="OpenAI settings" onClose={onClose}>
    <form className={formClass} onSubmit={save}>
      <p className="text-muted-foreground text-sm">Set up OpenAI for agent reasoning and document tools. Choose which reasoning models agents can use.</p>
      {!loaded && !error && <p role="status">Loading settings…</p>}
      {loaded && <>
        <Field id="openai-key" label="OpenAI API key">
          <Input id="openai-key" aria-label="OpenAI API key" type="password" autoComplete="new-password" value={key} disabled={busy} onChange={(e) => { setKey(e.target.value); setSaved(false); }} placeholder={loaded.configured ? "Key configured — leave blank to keep it" : "Enter an OpenAI API key"} />
          <p className="text-muted-foreground text-xs">Your key is stored encrypted on the server.</p>
        </Field>
        <Field id="openai-models" label="Allowed models">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button id="openai-models" type="button" variant="outline" disabled={busy} className="w-full justify-between font-normal" aria-label="Choose allowed models">
                {models.length} {models.length === 1 ? "model" : "models"} selected<ChevronDownIcon className="size-4 text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-[var(--radix-dropdown-menu-trigger-width)]">
              <DropdownMenuLabel>Choose models for your agents</DropdownMenuLabel>
              {options.map((model) => <DropdownMenuCheckboxItem key={model.id} checked={models.includes(model.id)}
                disabled={busy || (models.length === 1 && models.includes(model.id)) || (models.length >= 12 && !models.includes(model.id))}
                onSelect={(event) => event.preventDefault()} onCheckedChange={(checked) => toggleModel(model.id, checked)}>
                <span className="min-w-0"><span className="block truncate">{model.label}</span>{model.label !== model.id && <span className="text-muted-foreground block truncate text-xs">{model.id}</span>}</span>
              </DropdownMenuCheckboxItem>)}
            </DropdownMenuContent>
          </DropdownMenu>
          <div className="flex flex-wrap gap-1.5" aria-label="Selected models">{models.map((id) => <Badge key={id} variant="secondary" className="max-w-full whitespace-normal break-all">{labelFor(id)}</Badge>)}</div>
          <p className="text-muted-foreground text-xs">Select at least one. Model access is checked when you save.</p>
        </Field>
        <Field id="openai-default" label="Default model">
          <Select value={defaultModel} onValueChange={(value) => { setDefaultModel(value); setSaved(false); }} disabled={busy}>
            <SelectTrigger id="openai-default" aria-label="Default model" className="w-full"><SelectValue placeholder="Choose a default model" /></SelectTrigger>
            <SelectContent position="popper">{models.map((id) => <SelectItem key={id} value={id}>{labelFor(id)}</SelectItem>)}</SelectContent>
          </Select>
          <p className="text-muted-foreground text-xs">Used when an agent has no allowed model selected. Image generation is configured separately in Providers & APIs.</p>
        </Field>
        <p className="text-muted-foreground text-xs">Saving makes small billable API calls to verify image understanding, tool calling, and access for each selected model.</p>
      </>}
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      {saved && <Alert><AlertDescription>Connection verified and settings saved.</AlertDescription></Alert>}
      <div className={actionsClass}>
        <Button type="button" variant="outline" onClick={onClose}>Close</Button>
        <Button type="submit" disabled={!loaded || busy || models.length === 0 || !models.includes(defaultModel)}>{busy ? "Verifying models…" : "Validate & save"}</Button>
      </div>
    </form>
  </ModalShell>;
}
