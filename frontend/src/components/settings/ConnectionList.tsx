import { api } from "@/api";
import type { ProviderConnection, ProviderManifest, ProviderOperation } from "@/types";
import { Button } from "@/components/ui/button";
import type { ConnectionDraft, Perform } from "./provider-settings";

function ConnectionCard({ connection, manifest, busy, perform, liveProbe, onLiveProbe, onEdit }: {
  connection: ProviderConnection;
  manifest: ProviderManifest | undefined;
  busy: boolean;
  perform: Perform;
  liveProbe: boolean;
  onLiveProbe: (enabled: boolean) => void;
  onEdit: (draft: ConnectionDraft) => void;
}) {
  const revision = connection.revisions[0];
  const v = revision.verification;
  const url = `/api/settings/connections/${connection.id}`;
  const changeState = (action: "retire" | "restore" | "revoke", message: string) => void perform(
    () => api("POST", `${url}/state`, { action, expected_revision: connection.current_revision, expected_status: connection.status }),
    message,
  );
  const dependencies = Object.entries(connection.dependencies)
    .filter(([, count]) => Array.isArray(count) ? count.length : count)
    .map(([name, count]) => `${name.replaceAll("_", " ")}: ${Array.isArray(count) ? count.length : count}`)
    .join("; ");

  return (
    <div className="border-border flex flex-col gap-2 rounded-lg border p-3 text-sm">
      <p className="font-medium">{connection.label} · {connection.provider} · r{connection.current_revision}</p>
      <p>
        {connection.status} · {v.legacy ? "Existing imported connection" : v.passed ? "Verified" : "Not verified"} · {revision.has_secret ? "Key saved" : "No key"}
      </p>
      <p className="text-muted-foreground text-xs">
        Capabilities: {manifest?.capabilities.join(", ") || "service connection"}. Dependencies: {dependencies || "none"}.
      </p>
      {connection.status === "active" && <>
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => onEdit({
              connection,
              provider: connection.provider,
              label: connection.label,
              key: "",
              config: Object.fromEntries(Object.entries(revision.config).filter(([key]) => manifest?.fields.includes(key))),
            })}
          >
            Rotate key / configure
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void perform(() => api("POST", `${url}/verify`, { revision: connection.current_revision, live_computer: liveProbe }),
              "Verification queued in the worker. Model and image checks make billable requests.")}
          >
            Verify{liveProbe ? " with test computer" : " connection"}
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => changeState("retire", "Retired for new work; retained dependencies can still use it.")}>
            Retire
          </Button>
        </div>
        {["tenki", "e2b"].includes(connection.provider) && (
          <label className="text-xs">
            <input type="checkbox" checked={liveProbe} onChange={(e) => onLiveProbe(e.target.checked)} /> Explicitly run a billable isolated VM test: network, desktop, files and pause/resume, followed by cleanup. Required for new computer connections.
          </label>
        )}
      </>}
      {connection.status === "retired" && (
        <div className="flex flex-wrap gap-2">
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => void perform(() => api("POST", `${url}/release-indexes`), "Unused remote index cleanup queued; canonical documents stay readable.")}
          >
            Release unused remote indexes
          </Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => changeState("restore", "Connection restored.")}>Restore</Button>
          <Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("DELETE", url), "Unused connection removed.")}>
            Remove unused connection
          </Button>
        </div>
      )}
      <details>
        <summary className="text-muted-foreground cursor-pointer text-xs">Emergency revocation</summary>
        <p className="my-2 text-xs">Revocation blocks existing work and remote cleanup immediately. Reconnecting requires a new verified connection.</p>
        <Button
          size="sm"
          variant="destructive"
          disabled={busy || connection.status === "revoked"}
          onClick={() => changeState("revoke", "Connection revoked. Affected tasks retain their progress.")}
        >
          Revoke connection now
        </Button>
      </details>
    </div>
  );
}

function OperationStatus({ operation, busy, perform }: { operation: ProviderOperation; busy: boolean; perform: Perform }) {
  const url = `/api/settings/provider-operations/${operation.id}`;
  return (
    <div className="border-border rounded border p-2 text-sm" role="status">
      Verification: {operation.status}. {operation.result.error}
      {operation.status === "uncertain" && !operation.result.probe_name && (
        <Button
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => void perform(() => api("POST", `${url}/acknowledge`),
            "Interrupted check acknowledged. Review provider billing before initiating a separate verification.")}
        >
          Acknowledge uncertain check
        </Button>
      )}
      {operation.result.probe_name && ["failed", "uncertain"].includes(operation.status) && (
        <Button size="sm" variant="outline" disabled={busy} onClick={() => void perform(() => api("POST", `${url}/cleanup`), "Probe cleanup queued.")}>
          Clean up isolated test
        </Button>
      )}
    </div>
  );
}

/** Named provider connections and their verification checks. */
export default function ConnectionList({ connections, operations, catalog, busy, perform, liveProbe, onLiveProbe, onEdit }: {
  connections: ProviderConnection[];
  operations: ProviderOperation[];
  catalog: ProviderManifest[];
  busy: boolean;
  perform: Perform;
  liveProbe: Record<string, boolean>;
  onLiveProbe: (connectionId: string, enabled: boolean) => void;
  onEdit: (draft: ConnectionDraft) => void;
}) {
  return (
    <section className="flex flex-col gap-3" aria-label="Provider connections">
      <div className="flex items-center justify-between">
        <h3 className="font-semibold">Named connections</h3>
        <Button variant="outline" onClick={() => onEdit({ provider: "openai", label: "", key: "", config: {} })}>Add connection</Button>
      </div>
      {connections.map((connection) => (
        <ConnectionCard
          key={connection.id}
          connection={connection}
          manifest={catalog.find((m) => m.id === connection.provider)}
          busy={busy}
          perform={perform}
          liveProbe={!!liveProbe[connection.id]}
          onLiveProbe={(enabled) => onLiveProbe(connection.id, enabled)}
          onEdit={onEdit}
        />
      ))}
      {operations.map((operation) => <OperationStatus key={operation.id} operation={operation} busy={busy} perform={perform} />)}
    </section>
  );
}
