import { useEffect, useState, type FormEvent } from "react";
import { api } from "@/api";
import Field from "@/components/shared/Field";
import ModalShell, { actionsClass, formClass } from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription } from "@/components/ui/alert";

interface Settings { configured: boolean; models: string[]; default_model: string }

export default function OpenAISettingsModal({ onClose }: { onClose: () => void }) {
  const [loaded, setLoaded] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [models, setModels] = useState("");
  const [defaultModel, setDefaultModel] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => {
    let stopped = false;
    api<Settings>("GET", "/api/settings/openai").then((value) => {
      if (!stopped) { setLoaded(value); setModels(value.models.join("\n")); setDefaultModel(value.default_model); }
    }).catch((e: Error) => { if (!stopped) setError(e.message); });
    return () => { stopped = true; };
  }, []);
  const save = async (event: FormEvent) => {
    event.preventDefault();
    setBusy(true); setError(""); setSaved(false);
    try {
      const result = await api<Settings>("PUT", "/api/settings/openai", {
        api_key: key || null, models: models.split(/[\s,]+/).filter(Boolean), default_model: defaultModel.trim(),
      });
      setLoaded(result); setKey(""); setSaved(true);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  return <ModalShell title="OpenAI settings" onClose={onClose}>
    <form className={formClass} onSubmit={save}>
      <p className="text-muted-foreground text-sm">One connection for agent reasoning, document search, and data analysis. Keys stay encrypted on the server.</p>
      {!loaded && !error && <p role="status">Loading settings…</p>}
      {loaded && <>
        <Field id="openai-key" label="OpenAI API key"><Input id="openai-key" aria-label="OpenAI API key" type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} placeholder={loaded.configured ? "Key configured — leave blank to keep it" : "Enter an OpenAI API key"} /></Field>
        <Field id="openai-models" label="Allowed models"><Textarea id="openai-models" aria-label="Allowed models" value={models} onChange={(e) => setModels(e.target.value)} rows={4} /></Field>
        <Field id="openai-default" label="Default model"><Input id="openai-default" aria-label="Default model" value={defaultModel} onChange={(e) => setDefaultModel(e.target.value)} required /></Field>
        <p className="text-muted-foreground text-xs">One model ID per line. Saving makes small billable API calls to verify image input, tool calling, and model access.</p>
      </>}
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      {saved && <Alert><AlertDescription>Connection verified and settings saved.</AlertDescription></Alert>}
      <div className={actionsClass}>
        <Button type="button" variant="outline" onClick={onClose}>Close</Button>
        <Button type="submit" disabled={!loaded || busy}>{busy ? "Verifying models…" : "Validate & save"}</Button>
      </div>
    </form>
  </ModalShell>;
}
