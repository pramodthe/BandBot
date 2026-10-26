import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import type { Agent, ProviderManifest, ProviderSettings, ServiceBindings, ServiceChangeImpact, ServiceSelection } from "@/types";
import ModalShell from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Alert, AlertDescription } from "@/components/ui/alert";
import OpenAISettingsModal from "./OpenAISettingsModal";
import ServiceDefaults from "./ServiceDefaults";
import ConnectionList from "./ConnectionList";
import ConnectionForm from "./ConnectionForm";
import AgentOverrides from "./AgentOverrides";
import { serviceChoices, type ConnectionDraft, type Perform } from "./provider-settings";

export default function ProvidersModal({ onClose, agents }: { onClose: () => void; agents: Agent[] }) {
  const [openAISetup, setOpenAISetup] = useState(false);
  const [data, setData] = useState<ProviderSettings | null>(null);
  const [catalog, setCatalog] = useState<ProviderManifest[]>([]);
  const [draft, setDraft] = useState<ServiceBindings>({});
  const [draftVersion, setDraftVersion] = useState(0);
  const [clearOverrides, setClearOverrides] = useState(false);
  const [impact, setImpact] = useState<ServiceChangeImpact | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<ConnectionDraft | null>(null);
  const [liveProbe, setLiveProbe] = useState<Record<string, boolean>>({});
  const [agentId, setAgentId] = useState("");
  const [agentSelection, setAgentSelection] = useState<ServiceSelection>({ version: 0, bindings: {} });

  const reload = useCallback(async (reset = false) => {
    const [settings, manifests] = await Promise.all([
      api<ProviderSettings>("GET", "/api/settings/providers"),
      api<{ providers: ProviderManifest[] }>("GET", "/api/providers"),
    ]);
    setData(settings);
    setCatalog(manifests.providers);
    if (reset) {
      setDraft(settings.bindings);
      setDraftVersion(settings.version);
      setImpact(null);
    }
  }, []);

  useEffect(() => { void reload(true).catch((e: Error) => setError(e.message)); }, [reload]);

  // Poll while verification checks or document index changes are in flight.
  useEffect(() => {
    const pending = data?.operations.some((o) => o.status === "pending" || o.status === "running")
      || data?.changes.some((c) => c.status === "building");
    if (!pending) return;
    const timer = window.setInterval(() => { void reload().catch((e: Error) => setError(e.message)); }, 2000);
    return () => window.clearInterval(timer);
  }, [data, reload]);

  useEffect(() => {
    if (!agentId) return;
    let stopped = false;
    api<ServiceSelection>("GET", `/api/settings/agents/${agentId}/services`)
      .then((selection) => { if (!stopped) setAgentSelection(selection); })
      .catch((e: Error) => setError(e.message));
    return () => { stopped = true; };
  }, [agentId]);

  const perform: Perform = async (operation, message, reset = false) => {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      await operation();
      await reload(reset);
      setNotice(message);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveConnection = async () => {
    if (!form) return;
    await perform(async () => {
      if (form.connection) {
        await api("PATCH", `/api/settings/connections/${form.connection.id}`,
          { expected_revision: form.connection.current_revision, api_key: form.key || null, config: form.config });
      } else {
        await api("POST", "/api/settings/connections", { provider: form.provider, label: form.label, api_key: form.key, config: form.config });
      }
      setForm(null);
    }, "Saved. Verify the new revision before selecting it. Existing tasks keep the previous revision.");
  };

  if (openAISetup) {
    return <OpenAISettingsModal onClose={() => {
      setOpenAISetup(false);
      void reload(true).catch((e: Error) => setError(e.message));
    }} />;
  }

  return (
    <ModalShell title="Providers & APIs" wide onClose={onClose}>
      <div className="flex flex-col gap-5">
        <p className="text-muted-foreground text-sm">
          Choose each service independently. Activation affects new work; saved tasks and computers keep their original
          connection. Keys are encrypted and write-only.
        </p>
        <Button variant="outline" className="self-start" onClick={() => setOpenAISetup(true)}>OpenAI quick setup</Button>
        {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
        {notice && <p role="status" className="text-sm">{notice}</p>}
        {!data && <p>Loading provider settings…</p>}
        {data && <>
          <ServiceDefaults
            data={data}
            catalog={catalog}
            draft={draft}
            draftVersion={draftVersion}
            clearOverrides={clearOverrides}
            impact={impact}
            busy={busy}
            perform={perform}
            onDraft={(bindings) => { setDraft(bindings); setImpact(null); }}
            onClearOverrides={(clear) => { setClearOverrides(clear); setImpact(null); }}
            onImpact={setImpact}
            onReload={() => void reload(true)}
            onNotice={setNotice}
          />
          <ConnectionList
            connections={data.connections}
            operations={data.operations}
            catalog={catalog}
            busy={busy}
            perform={perform}
            liveProbe={liveProbe}
            onLiveProbe={(connectionId, enabled) => setLiveProbe({ ...liveProbe, [connectionId]: enabled })}
            onEdit={setForm}
          />
          {form && (
            <ConnectionForm
              form={form}
              catalog={catalog}
              busy={busy}
              onChange={setForm}
              onSave={() => void saveConnection()}
              onCancel={() => setForm(null)}
            />
          )}
          <AgentOverrides
            agents={agents}
            agentId={agentId}
            selection={agentSelection}
            choices={serviceChoices(data.connections, catalog, "llm")}
            busy={busy}
            perform={perform}
            onAgent={setAgentId}
            onSelection={setAgentSelection}
          />
        </>}
      </div>
    </ModalShell>
  );
}
