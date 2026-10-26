import type { ProviderManifest } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { FIELD_LABELS, NUMERIC_FIELDS, selectClass, type ConnectionDraft } from "./provider-settings";

/** Adds a connection, or a new revision of one, without verifying it. */
export default function ConnectionForm({ form, catalog, busy, onChange, onSave, onCancel }: {
  form: ConnectionDraft;
  catalog: ProviderManifest[];
  busy: boolean;
  onChange: (form: ConnectionDraft) => void;
  onSave: () => void;
  onCancel: () => void;
}) {
  const manifest = catalog.find((m) => m.id === form.provider);
  const setConfig = (field: string, value: unknown) => onChange({ ...form, config: { ...form.config, [field]: value } });

  return (
    <section className="border-border flex flex-col gap-3 rounded-lg border p-3" aria-label="Connection editor">
      <h3 className="font-semibold">{form.connection ? "New connection revision" : "Add connection"}</h3>
      {!form.connection && <>
        <label className="text-sm">
          Provider
          <select
            className={selectClass}
            aria-label="Provider"
            value={form.provider}
            onChange={(e) => onChange({ ...form, provider: e.target.value, config: {} })}
          >
            {catalog.map((m) => <option key={m.id} value={m.id}>{m.label}</option>)}
          </select>
        </label>
        <label className="text-sm">
          Connection name
          <Input value={form.label} onChange={(e) => onChange({ ...form, label: e.target.value })} />
        </label>
      </>}
      {manifest?.secret_required !== false && (
        <label className="text-sm">
          API key
          <Input
            type="password"
            autoComplete="new-password"
            value={form.key}
            placeholder={form.connection ? "Leave blank to retain the current key" : "Enter API key"}
            onChange={(e) => onChange({ ...form, key: e.target.value })}
          />
        </label>
      )}
      {form.provider === "compatible" && (
        <label className="text-sm">
          API key (optional)
          <Input type="password" autoComplete="new-password" value={form.key} onChange={(e) => onChange({ ...form, key: e.target.value })} />
        </label>
      )}
      {manifest?.fields.map((field) => (
        <label key={field} className="text-sm">
          {FIELD_LABELS[field] ?? field}
          {field === "models" ? (
            <Textarea
              value={((form.config.models as string[]) ?? []).join("\n")}
              onChange={(e) => setConfig("models", e.target.value.split(/[\s,]+/).filter(Boolean))}
            />
          ) : field === "sharing" ? (
            <select className={selectClass} value={String(form.config[field] ?? "user")} onChange={(e) => setConfig(field, e.target.value)}>
              <option value="user">One private computer per user; separate agent screens</option>
              <option value="workspace">Legacy shared workspace; private chats separate</option>
              <option value="agent_channel">Separate per agent and channel</option>
            </select>
          ) : (
            <Input
              type={NUMERIC_FIELDS.has(field) ? "number" : "text"}
              value={String(form.config[field] ?? "")}
              onChange={(e) => setConfig(field, NUMERIC_FIELDS.has(field) ? Number(e.target.value) : e.target.value)}
            />
          )}
        </label>
      ))}
      <p className="text-muted-foreground text-xs">
        Models must pass vision and tool continuation checks. Public endpoints require HTTPS. Local/private endpoints must
        first be allowlisted by the server operator. Computer revisions create a separate destination; files are not
        transferred.
      </p>
      <div className="flex gap-2">
        <Button disabled={busy} onClick={onSave}>Save unverified revision</Button>
        <Button variant="outline" onClick={onCancel}>Cancel</Button>
      </div>
    </section>
  );
}
