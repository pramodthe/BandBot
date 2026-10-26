import { api } from "@/api";
import type { ProviderManifest, ProviderSettings, ServiceBindings, ServiceChangeImpact } from "@/types";
import { Button } from "@/components/ui/button";
import ServiceSelect from "./ServiceSelect";
import { SERVICES, selectClass, serviceChoices, type Perform } from "./provider-settings";

/** Workspace-wide service selection: draft, preview the impact, then activate. */
export default function ServiceDefaults({
  data, catalog, draft, draftVersion, clearOverrides, impact, busy, perform,
  onDraft, onClearOverrides, onImpact, onReload, onNotice,
}: {
  data: ProviderSettings;
  catalog: ProviderManifest[];
  draft: ServiceBindings;
  draftVersion: number;
  clearOverrides: boolean;
  impact: ServiceChangeImpact | null;
  busy: boolean;
  perform: Perform;
  /** Replace the draft; any previewed impact no longer applies. */
  onDraft: (bindings: ServiceBindings) => void;
  onClearOverrides: (clear: boolean) => void;
  onImpact: (impact: ServiceChangeImpact) => void;
  onReload: () => void;
  onNotice: (message: string) => void;
}) {
  const proposal = () => ({
    expected_version: draftVersion,
    bindings: Object.fromEntries(Object.keys(SERVICES).map((service) => [service, draft[service] ?? { disabled: true }])),
    clear_computer_overrides: clearOverrides,
  });
  const llmRevision = data.connections
    .find((c) => c.id === draft.llm?.connection_id)?.revisions
    .find((r) => r.revision === draft.llm?.revision);
  const hostedCodeVerified = !!llmRevision?.verification.legacy;

  return (
    <section className="flex flex-col gap-3" aria-label="Service defaults">
      <h3 className="font-semibold">Workspace service defaults</h3>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={() => void perform(() => api("POST", "/api/settings/document-indexes/retry"),
          "Definite index errors queued for retry. Uncertain uploads retain their reconciliation state.")}
      >
        Retry failed document indexes
      </Button>
      {Object.entries(SERVICES).map(([service, label]) => (
        <label key={service} className="flex flex-col gap-1 text-sm">
          {label}
          <ServiceSelect
            service={service}
            value={draft[service]}
            choices={serviceChoices(data.connections, catalog, service)}
            onChange={(binding) => onDraft({ ...draft, [service]: binding })}
          />
        </label>
      ))}
      {draft.llm?.provider === "openai" && (
        <label className="text-sm">
          <input
            type="checkbox"
            disabled={!hostedCodeVerified}
            checked={draft.llm.hosted_code !== false && hostedCodeVerified}
            onChange={(e) => onDraft({ ...draft, llm: { ...draft.llm, hosted_code: e.target.checked } })}
          /> Allow verified OpenAI hosted execution. Other models use the selected computer.
        </label>
      )}
      <p className="text-muted-foreground text-xs">
        Changing a computer connection creates a separate computer. Existing files, disks and browser sessions stay with
        the original computer. Composio and operator-configured MCP access still follow channel approvals and agent
        permissions.
      </p>
      {data.computer_overrides > 0 && (
        <label className="text-sm">
          <input type="checkbox" checked={clearOverrides} onChange={(e) => onClearOverrides(e.target.checked)} /> Replace {data.computer_overrides} existing agent/channel computer overrides for new work
        </label>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          disabled={busy}
          onClick={() => void perform(async () => onImpact(await api<ServiceChangeImpact>("POST", "/api/settings/services/preview", proposal())),
            "Review the affected services before activation.")}
        >
          Preview change
        </Button>
        <Button variant="outline" disabled={busy} onClick={onReload}>Reload saved settings</Button>
      </div>
      {impact && (
        <div className="border-border rounded-lg border p-3 text-sm">
          <p>
            Changes: {impact.changed_services.map((s) => SERVICES[s]).join(", ") || "none"}. {impact.preserved_tasks} unfinished tasks retain their bindings.
          </p>
          <p>
            {impact.documents_to_reindex
              ? `${impact.documents_to_reindex} documents will be indexed before activation. Current settings remain active until complete.`
              : impact.note}
          </p>
          <Button
            className="mt-2"
            disabled={busy}
            onClick={() => void perform(() => api("PUT", "/api/settings/services", proposal()),
              "Selection submitted. Check index preparation below if required.", true)}
          >
            Activate reviewed selection
          </Button>
        </div>
      )}
      {data.changes.map((change) => (
        <p key={change.id} role="status" className="text-sm">Document index change: {change.status}. {change.error}</p>
      ))}
      {data.history.length > 0 && (
        <label className="text-sm">
          Restore previous selection
          <select
            aria-label="Restore previous selection"
            className={selectClass}
            defaultValue=""
            onChange={(e) => {
              const history = data.history.find((h) => h.version === Number(e.target.value));
              if (history) {
                onDraft(history.bindings);
                onNotice("Previous selection loaded. Preview and activate it to restore.");
              }
            }}
          >
            <option value="">Choose a saved selection…</option>
            {data.history.map((h) => <option key={h.version} value={h.version}>Selection {h.version}</option>)}
          </select>
        </label>
      )}
    </section>
  );
}
