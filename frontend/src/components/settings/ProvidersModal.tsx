import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import type { Agent } from "@/types";
import ModalShell from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Alert, AlertDescription } from "@/components/ui/alert";

type Binding = { connection_id?: string; revision?: number; provider?: string; model?: string; disabled?: boolean; hosted_code?: boolean };
type Bindings = Record<string, Binding>;
interface Verification { passed?: boolean; legacy?: boolean; models?: Record<string, { tools?: boolean; vision?: boolean }>; image_model?: string; pause_resume?: boolean }
interface Revision { revision: number; config: Record<string, unknown>; verification: Verification; has_secret: boolean }
interface Connection { id: string; provider: string; label: string; status: string; current_revision: number; revisions: Revision[]; dependencies: Record<string, number | string[]> }
interface Manifest { id: string; label: string; services: string[]; fields: string[]; capabilities: string[]; secret_required: boolean }
interface Operation { id: string; status: string; result: { error?: string; cleanup?: string; probe_name?: string }; connection_id?: string }
interface Settings { version: number; bindings: Bindings; connections: Connection[]; history: { version: number; bindings: Bindings }[]; computer_overrides: number; changes: { id: string; status: string; error: string }[]; operations: Operation[] }
interface Impact { changed_services: string[]; preserved_tasks: number; documents_to_reindex: number; note: string }
const services: Record<string, string> = { llm: "Agent reasoning", computer: "Cloud computers", research: "Web research", retrieval: "Document search", document_analysis: "Document analysis & scan transcription", images: "Image generation", integrations: "App integrations" };
const labels: Record<string, string> = { models: "Model IDs (one per line)", image_model: "Image model ID", base_url: "Endpoint URL", context_window: "Model context window (tokens)", image: "Prepared desktop image", sharing: "Computer sharing", disk_gib: "Persistent disk (GiB)", timeout: "Computer timeout (seconds)", pause_retention: "Pause retention (seconds)", template: "Desktop template" };
const numeric = new Set(["context_window", "disk_gib", "timeout", "pause_retention"]);
const selectClass = "border-input bg-background w-full min-w-0 rounded-md border px-3 py-2 text-sm";

export default function ProvidersModal({ onClose, agents }: { onClose: () => void; agents: Agent[] }) {
  const [data, setData] = useState<Settings | null>(null);
  const [catalog, setCatalog] = useState<Manifest[]>([]);
  const [draft, setDraft] = useState<Bindings>({});
  const [draftVersion, setDraftVersion] = useState(0);
  const [clearOverrides, setClearOverrides] = useState(false);
  const [impact, setImpact] = useState<Impact | null>(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState<{ provider: string; label: string; key: string; config: Record<string, unknown>; connection?: Connection } | null>(null);
  const [liveProbe, setLiveProbe] = useState<Record<string, boolean>>({});
  const [agentId, setAgentId] = useState("");
  const [agentSelection, setAgentSelection] = useState<{ version: number; bindings: Bindings }>({ version: 0, bindings: {} });
  const reload = useCallback(async (reset = false) => {
    const [settings, manifests] = await Promise.all([api<Settings>("GET", "/api/settings/providers"), api<{ providers: Manifest[] }>("GET", "/api/providers")]);
    setData(settings); setCatalog(manifests.providers);
    if (reset) { setDraft(settings.bindings); setDraftVersion(settings.version); setImpact(null); }
  }, []);
  useEffect(() => { void reload(true).catch((e: Error) => setError(e.message)); }, [reload]);
  useEffect(() => {
    if (!data?.operations.some((o) => o.status === "pending" || o.status === "running") && !data?.changes.some((c) => c.status === "building")) return;
    const timer = window.setInterval(() => { void reload().catch((e: Error) => setError(e.message)); }, 2000);
    return () => window.clearInterval(timer);
  }, [data, reload]);
  useEffect(() => {
    if (!agentId) return;
    let stopped = false;
    api<{ version: number; bindings: Bindings }>("GET", `/api/settings/agents/${agentId}/services`).then((v) => { if (!stopped) setAgentSelection(v); }).catch((e: Error) => setError(e.message));
    return () => { stopped = true; };
  }, [agentId]);
  const perform = async (operation: () => Promise<unknown>, message: string, reset = false) => {
    setBusy(true); setError(""); setNotice("");
    try { await operation(); await reload(reset); setNotice(message); }
    catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  };
  const choices = (service: string) => (data?.connections ?? []).flatMap((connection) => connection.revisions.flatMap((revision) => {
    const manifest = catalog.find((m) => m.id === connection.provider);
    const v = revision.verification;
    if (connection.status !== "active" || !manifest?.services.includes(service) || !(v.passed || v.legacy)) return [];
    if (service === "computer" && !(v.legacy || v.pause_resume)) return [];
    const models = service === "llm" || service === "document_analysis" ? Object.entries(v.models ?? {}).filter(([, test]) => test.tools && test.vision).map(([m]) => m) : service === "images" ? (v.image_model ? [v.image_model] : []) : [""];
    return models.map((model) => ({ binding: { connection_id: connection.id, revision: revision.revision, provider: connection.provider, ...(model ? { model } : {}) }, label: `${connection.label} · r${revision.revision}${model ? ` · ${model}` : ""}` }));
  }));
  const bindingKey = (b?: Binding) => b?.disabled || !b?.connection_id ? "" : `${b.connection_id}:${b.revision}:${b.model ?? ""}`;
  const selector = (service: string, value: Binding | undefined, change: (b: Binding) => void, inherit = false) => <select aria-label={inherit ? "Agent model override" : services[service]} className={selectClass} value={bindingKey(value)} onChange={(e) => change(choices(service).find((c) => bindingKey(c.binding) === e.target.value)?.binding ?? { disabled: true })}>
    <option value="">{inherit ? "Follow workspace default" : "Disabled / not configured"}</option>
    {value?.connection_id && !choices(service).some((c) => bindingKey(c.binding) === bindingKey(value)) && <option value={bindingKey(value)}>Unavailable selection · {value.provider} · r{value.revision}</option>}
    {choices(service).map((choice) => <option key={bindingKey(choice.binding)} value={bindingKey(choice.binding)}>{choice.label}</option>)}
  </select>;
  const saveConnection = async () => {
    if (!form) return;
    await perform(async () => {
      if (form.connection) await api("PATCH", `/api/settings/connections/${form.connection.id}`, { expected_revision: form.connection.current_revision, api_key: form.key || null, config: form.config });
      else await api("POST", "/api/settings/connections", { provider: form.provider, label: form.label, api_key: form.key, config: form.config });
      setForm(null);
    }, "Saved. Verify the new revision before selecting it. Existing tasks keep the previous revision.");
  };
  return <ModalShell title="Providers & APIs" wide onClose={onClose}>
    <div className="flex flex-col gap-5">
      <p className="text-muted-foreground text-sm">Choose each service independently. Activation affects new work; saved tasks and computers keep their original connection. Keys are encrypted and write-only.</p>
      {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
      {notice && <p role="status" className="text-sm">{notice}</p>}
      {!data && <p>Loading provider settings…</p>}
      {data && <>
        <section className="flex flex-col gap-3" aria-label="Service defaults">
          <h3 className="font-semibold">Workspace service defaults</h3><Button variant="outline" size="sm" disabled={busy} onClick={() => void perform(() => api("POST", "/api/settings/document-indexes/retry"), "Definite index errors queued for retry. Uncertain uploads retain their reconciliation state.")}>Retry failed document indexes</Button>
          {Object.entries(services).map(([service, label]) => <label key={service} className="flex flex-col gap-1 text-sm">{label}{selector(service, draft[service], (b) => { setDraft({ ...draft, [service]: b }); setImpact(null); })}</label>)}
          {draft.llm?.provider === "openai" && <label className="text-sm"><input type="checkbox" disabled={!data.connections.find((c) => c.id === draft.llm.connection_id)?.revisions.find((r) => r.revision === draft.llm.revision)?.verification.legacy} checked={draft.llm.hosted_code !== false && !!data.connections.find((c) => c.id === draft.llm.connection_id)?.revisions.find((r) => r.revision === draft.llm.revision)?.verification.legacy} onChange={(e) => { setDraft({ ...draft, llm: { ...draft.llm, hosted_code: e.target.checked } }); setImpact(null); }} /> Allow verified OpenAI hosted execution. Other models use the selected computer.</label>}
          <p className="text-muted-foreground text-xs">Changing a computer connection creates a separate computer. Existing files, disks and browser sessions stay with the original computer. Composio and operator-configured MCP access still follow channel approvals and agent permissions.</p>
          {data.computer_overrides > 0 && <label className="text-sm"><input type="checkbox" checked={clearOverrides} onChange={(e) => { setClearOverrides(e.target.checked); setImpact(null); }} /> Replace {data.computer_overrides} existing agent/channel computer overrides for new work</label>}
          <div className="flex flex-wrap gap-2">
            <Button disabled={busy} onClick={() => void perform(async () => setImpact(await api<Impact>("POST", "/api/settings/services/preview", { expected_version: draftVersion, bindings: Object.fromEntries(Object.keys(services).map((s) => [s, draft[s] ?? { disabled: true }])), clear_computer_overrides: clearOverrides })), "Review the affected services before activation.")}>Preview change</Button>
            <Button variant="outline" disabled={busy} onClick={() => void reload(true)}>Reload saved settings</Button>
          </div>
          {impact && <div className="border-border rounded-lg border p-3 text-sm"><p>Changes: {impact.changed_services.map((s) => services[s]).join(", ") || "none"}. {impact.preserved_tasks} unfinished tasks retain their bindings.</p><p>{impact.documents_to_reindex ? `${impact.documents_to_reindex} documents will be indexed before activation. Current settings remain active until complete.` : impact.note}</p><Button className="mt-2" disabled={busy} onClick={() => void perform(() => api("PUT", "/api/settings/services", { expected_version: draftVersion, bindings: Object.fromEntries(Object.keys(services).map((s) => [s, draft[s] ?? { disabled: true }])), clear_computer_overrides: clearOverrides }), "Selection submitted. Check index preparation below if required.", true)}>Activate reviewed selection</Button></div>}
          {data.changes.map((change) => <p key={change.id} role="status" className="text-sm">Document index change: {change.status}. {change.error}</p>)}
          {data.history.length > 0 && <label className="text-sm">Restore previous selection<select aria-label="Restore previous selection" className={selectClass} defaultValue="" onChange={(e) => { const history = data.history.find((h) => h.version === Number(e.target.value)); if (history) { setDraft(history.bindings); setImpact(null); setNotice("Previous selection loaded. Preview and activate it to restore."); } }}><option value="">Choose a saved selection…</option>{data.history.map((h) => <option key={h.version} value={h.version}>Selection {h.version}</option>)}</select></label>}
        </section>
        <section className="flex flex-col gap-3" aria-label="Provider connections">
          <div className="flex items-center justify-between"><h3 className="font-semibold">Named connections</h3><Button variant="outline" onClick={() => setForm({ provider: "openai", label: "", key: "", config: {} })}>Add connection</Button></div>
          {data.connections.map((connection) => { const revision = connection.revisions[0]; const v = revision.verification; return <div key={connection.id} className="border-border flex flex-col gap-2 rounded-lg border p-3 text-sm">
            <p className="font-medium">{connection.label} · {connection.provider} · r{connection.current_revision}</p>
            <p>{connection.status} · {v.legacy ? "Existing imported connection" : v.passed ? "Verified" : "Not verified"} · {revision.has_secret ? "Key saved" : "No key"}</p>
            <p className="text-muted-foreground text-xs">Capabilities: {catalog.find((m) => m.id === connection.provider)?.capabilities.join(", ") || "service connection"}. Dependencies: {Object.entries(connection.dependencies).filter(([, count]) => Array.isArray(count) ? count.length : count).map(([name, count]) => `${name.replaceAll("_", " ")}: ${Array.isArray(count) ? count.length : count}`).join("; ") || "none"}.</p>
            {connection.status === "active" && <><div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => setForm({ connection, provider: connection.provider, label: connection.label, key: "", config: Object.fromEntries(Object.entries(revision.config).filter(([key]) => catalog.find((m) => m.id === connection.provider)?.fields.includes(key))) })}>Rotate key / configure</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/connections/${connection.id}/verify`, { revision: connection.current_revision, live_computer: !!liveProbe[connection.id] }), "Verification queued in the worker. Model and image checks make billable requests.")}>Verify{liveProbe[connection.id] ? " with test computer" : " connection"}</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/connections/${connection.id}/state`, { action: "retire", expected_revision: connection.current_revision, expected_status: connection.status }), "Retired for new work; retained dependencies can still use it.")}>Retire</Button></div>{["tenki", "e2b"].includes(connection.provider) && <label className="text-xs"><input type="checkbox" checked={!!liveProbe[connection.id]} onChange={(e) => setLiveProbe({ ...liveProbe, [connection.id]: e.target.checked })} /> Explicitly run a billable isolated VM test: network, desktop, files and pause/resume, followed by cleanup. Required for new computer connections.</label>}</>}
            {connection.status === "retired" && <div className="flex flex-wrap gap-2"><Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/connections/${connection.id}/release-indexes`), "Unused remote index cleanup queued; canonical documents stay readable.")}>Release unused remote indexes</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/connections/${connection.id}/state`, { action: "restore", expected_revision: connection.current_revision, expected_status: connection.status }), "Connection restored.")}>Restore</Button><Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("DELETE", `/api/settings/connections/${connection.id}`), "Unused connection removed.")}>Remove unused connection</Button></div>}
            <details><summary className="text-muted-foreground cursor-pointer text-xs">Emergency revocation</summary><p className="my-2 text-xs">Revocation blocks existing work and remote cleanup immediately. Reconnecting requires a new verified connection.</p><Button size="sm" variant="destructive" disabled={busy || connection.status === "revoked"} onClick={() => void perform(() => api("POST", `/api/settings/connections/${connection.id}/state`, { action: "revoke", expected_revision: connection.current_revision, expected_status: connection.status }), "Connection revoked. Affected tasks retain their progress.")}>Revoke connection now</Button></details>
          </div>; })}
          {data.operations.map((operation) => <div key={operation.id} className="border-border rounded border p-2 text-sm" role="status">Verification: {operation.status}. {operation.result.error}{operation.status === "uncertain" && !operation.result.probe_name && <Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/provider-operations/${operation.id}/acknowledge`), "Interrupted check acknowledged. Review provider billing before initiating a separate verification.")}>Acknowledge uncertain check</Button>}{operation.result.probe_name && ["failed", "uncertain"].includes(operation.status) && <Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `/api/settings/provider-operations/${operation.id}/cleanup`), "Probe cleanup queued.")}>Clean up isolated test</Button>}</div>)}
        </section>
        {form && <section className="border-border flex flex-col gap-3 rounded-lg border p-3" aria-label="Connection editor"><h3 className="font-semibold">{form.connection ? "New connection revision" : "Add connection"}</h3>
          {!form.connection && <><label className="text-sm">Provider<select className={selectClass} aria-label="Provider" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value, config: {} })}>{catalog.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}</select></label><label className="text-sm">Connection name<Input value={form.label} onChange={(e) => setForm({ ...form, label: e.target.value })} /></label></>}
          {catalog.find((m) => m.id === form.provider)?.secret_required !== false && <label className="text-sm">API key<Input type="password" autoComplete="new-password" value={form.key} placeholder={form.connection ? "Leave blank to retain the current key" : "Enter API key"} onChange={(e) => setForm({ ...form, key: e.target.value })} /></label>}
          {form.provider === "compatible" && <label className="text-sm">API key (optional)<Input type="password" autoComplete="new-password" value={form.key} onChange={(e) => setForm({ ...form, key: e.target.value })} /></label>}
          {catalog.find((m) => m.id === form.provider)?.fields.map((field) => <label key={field} className="text-sm">{labels[field] ?? field}{field === "models" ? <Textarea value={((form.config.models as string[]) ?? []).join("\n")} onChange={(e) => setForm({ ...form, config: { ...form.config, models: e.target.value.split(/[\s,]+/).filter(Boolean) } })} /> : field === "sharing" ? <select className={selectClass} value={String(form.config[field] ?? "workspace")} onChange={(e) => setForm({ ...form, config: { ...form.config, [field]: e.target.value } })}><option value="workspace">Shared workspace; private chats separate</option><option value="agent_channel">Separate per agent and channel</option></select> : <Input type={numeric.has(field) ? "number" : "text"} value={String(form.config[field] ?? "")} onChange={(e) => setForm({ ...form, config: { ...form.config, [field]: numeric.has(field) ? Number(e.target.value) : e.target.value } })} />}</label>)}
          <p className="text-muted-foreground text-xs">Models must pass vision and tool continuation checks. Public endpoints require HTTPS. Local/private endpoints must first be allowlisted by the server operator. Computer revisions create a separate destination; files are not transferred.</p>
          <div className="flex gap-2"><Button disabled={busy} onClick={() => void saveConnection()}>Save unverified revision</Button><Button variant="outline" onClick={() => setForm(null)}>Cancel</Button></div>
        </section>}
        <section className="flex flex-col gap-2" aria-label="Agent model overrides"><h3 className="font-semibold">Agent model overrides</h3><select aria-label="Agent" className={selectClass} value={agentId} onChange={(e) => setAgentId(e.target.value)}><option value="">Choose an agent…</option>{agents.filter((a) => a.runtime === "deepagents").map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}</select>{agentId && <>{selector("llm", agentSelection.bindings.llm, (binding) => setAgentSelection({ ...agentSelection, bindings: binding.disabled ? {} : { llm: binding } }), true)}<Button variant="outline" disabled={busy} onClick={() => void perform(() => api("PUT", `/api/settings/agents/${agentId}/services`, { expected_version: agentSelection.version, bindings: agentSelection.bindings }).then((result) => setAgentSelection(result as typeof agentSelection)), "Agent selection saved for new tasks.")}>Save agent override</Button></>}</section>
      </>}
    </div>
  </ModalShell>;
}
