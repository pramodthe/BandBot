import { useState } from "react";
import type { MessageComponent } from "@/types";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import LiveAppPreview from "./LiveAppPreview";

export type AppPreviewComponent = Extract<MessageComponent, { kind: "app" }>;

export default function LiveAppPreviews({ apps, hasSavedPreview = false }: { apps: AppPreviewComponent[]; hasSavedPreview?: boolean }) {
  const choices = [...new Map(apps.map((app) => [app.props.previewId, app])).values()];
  const [selectedId, setSelectedId] = useState(choices[0]?.props.previewId ?? "");
  const selected = choices.find((app) => app.props.previewId === selectedId) ?? choices[0];
  if (!selected) return null;
  return <div className="flex min-w-0 flex-col gap-3">
    {choices.length > 1 ? <Select value={selected.props.previewId} onValueChange={setSelectedId}>
      <SelectTrigger aria-label="Choose live app" className="w-full sm:max-w-xs"><SelectValue /></SelectTrigger>
      <SelectContent>{choices.map((app) => <SelectItem key={app.props.previewId} value={app.props.previewId}>
        {app.props.title}{choices.some((other) => other.props.previewId !== app.props.previewId && other.props.title === app.props.title) ? ` · ${app.props.port}` : ""}
      </SelectItem>)}</SelectContent>
    </Select> : null}
    <LiveAppPreview key={selected.props.previewId} previewId={selected.props.previewId} title={selected.props.title} hasSavedPreview={hasSavedPreview} />
  </div>;
}
