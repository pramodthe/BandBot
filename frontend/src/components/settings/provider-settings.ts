import type { ProviderConnection, ProviderManifest, ServiceBinding } from "@/types";

/** Independently selectable services, in display order. */
export const SERVICES: Record<string, string> = {
  llm: "Agent reasoning",
  computer: "Cloud computers",
  research: "Web research",
  retrieval: "Document search",
  document_analysis: "Document analysis & scan transcription",
  images: "Image generation",
  integrations: "App integrations",
};

export const FIELD_LABELS: Record<string, string> = {
  models: "Model IDs (one per line)",
  image_model: "Image model ID",
  base_url: "Endpoint URL",
  context_window: "Model context window (tokens)",
  image: "Prepared desktop image",
  sharing: "Computer sharing",
  disk_gib: "Persistent disk (GiB)",
  timeout: "Computer timeout (seconds)",
  pause_retention: "Pause retention (seconds)",
  template: "Desktop template",
};

export const NUMERIC_FIELDS = new Set(["context_window", "disk_gib", "timeout", "pause_retention"]);

export const selectClass = "border-input bg-background w-full min-w-0 rounded-md border px-3 py-2 text-sm";

/** Runs an API action, reloads settings and shows `message` on success. */
export type Perform = (operation: () => Promise<unknown>, message: string, reset?: boolean) => Promise<void>;

/** The connection editor's state; `connection` is set when adding a revision to an existing one. */
export interface ConnectionDraft {
  provider: string;
  label: string;
  key: string;
  config: Record<string, unknown>;
  connection?: ProviderConnection;
}

export interface ServiceChoice {
  binding: ServiceBinding;
  label: string;
}

export const bindingKey = (binding?: ServiceBinding) =>
  binding?.disabled || !binding?.connection_id ? "" : `${binding.connection_id}:${binding.revision}:${binding.model ?? ""}`;

/** Verified connection revisions (and models) that can serve `service`. */
export function serviceChoices(connections: ProviderConnection[], catalog: ProviderManifest[], service: string): ServiceChoice[] {
  return connections.flatMap((connection) => connection.revisions.flatMap((revision) => {
    const manifest = catalog.find((m) => m.id === connection.provider);
    const v = revision.verification;
    if (connection.status !== "active" || !manifest?.services.includes(service) || !(v.passed || v.legacy)) return [];
    if (service === "computer" && !(v.legacy || v.pause_resume)) return [];
    const models = service === "llm" || service === "document_analysis"
      ? Object.entries(v.models ?? {}).filter(([, test]) => test.tools && test.vision).map(([model]) => model)
      : service === "images"
        ? (v.image_model ? [v.image_model] : [])
        : [""];
    return models.map((model) => ({
      binding: { connection_id: connection.id, revision: revision.revision, provider: connection.provider, ...(model ? { model } : {}) },
      label: `${connection.label} · r${revision.revision}${model ? ` · ${model}` : ""}`,
    }));
  }));
}
