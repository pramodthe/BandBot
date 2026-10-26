import { useEffect, useRef, useState } from "react";
import { DownloadIcon, FileCodeIcon, FileTextIcon, LoaderCircleIcon } from "lucide-react";
import { api } from "@/api";
import type { Artifact, ArtifactReference, TaskDetail } from "@/types";
import { Button } from "@/components/ui/button";
import ArtifactPreview from "./ArtifactPreview";
import { artifactName, downloadArtifact, isHtmlArtifact } from "./artifacts";
import LiveAppPreviews, { type AppPreviewComponent } from "./LiveAppPreviews";

export default function MessageArtifacts({ artifacts, liveApps = [] }: { artifacts: ArtifactReference[]; liveApps?: AppPreviewComponent[] }) {
  const [files, setFiles] = useState<Record<string, Artifact[]>>({});
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [visible, setVisible] = useState(false);
  const [revision, setRevision] = useState(0);
  const container = useRef<HTMLDivElement>(null);
  const taskIds = [...new Set(artifacts.map((artifact) => artifact.task_id))].join(",");
  const primaryHtml = artifacts.find((artifact) => isHtmlArtifact(artifact.path));

  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) { setVisible(true); observer.disconnect(); }
    }, { rootMargin: "200px" });
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    setError("");
    void Promise.all(taskIds.split(",").filter(Boolean).map(async (id) => {
      const result = await api<TaskDetail>("GET", `/api/tasks/${encodeURIComponent(id)}`);
      return [id, result.artifacts] as const;
    })).then((results) => { if (!cancelled) setFiles(Object.fromEntries(results)); })
      .catch((cause: Error) => { if (!cancelled) setError(cause.message); });
    return () => { cancelled = true; };
  }, [visible, taskIds, revision]);

  const download = async (reference: ArtifactReference) => {
    setBusy(reference.path); setError("");
    try {
      const loaded = files[reference.task_id] ?? (await api<TaskDetail>("GET", `/api/tasks/${encodeURIComponent(reference.task_id)}`)).artifacts;
      const file = loaded.find((item) => item.path === reference.path);
      if (!file) throw new Error("This exported file is no longer available.");
      downloadArtifact(file);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(null); }
  };

  return <div ref={container} className="mt-4 flex w-full min-w-0 flex-col gap-4" aria-label="Files created by this agent">
    {artifacts.map((reference) => {
      const html = isHtmlArtifact(reference.path);
      const file = files[reference.task_id]?.find((item) => item.path === reference.path);
      return <section key={`${reference.task_id}-${reference.path}`} className="flex min-w-0 flex-col gap-2" aria-label={`Created file ${artifactName(reference.path)}`}>
        <div className="flex min-w-0 items-center gap-2">
          {html ? <FileCodeIcon className="text-muted-foreground size-4 shrink-0" /> : <FileTextIcon className="text-muted-foreground size-4 shrink-0" />}
          <div className="min-w-0 flex-1"><p className="truncate text-sm font-medium">{html ? "Webpage preview" : artifactName(reference.path)}</p>{html ? <p className="text-muted-foreground truncate text-xs">{artifactName(reference.path)}</p> : null}</div>
          <Button variant="ghost" size="icon-sm" aria-label={`Download ${artifactName(reference.path)}`} disabled={busy === reference.path} onClick={() => void download(reference)}><DownloadIcon /></Button>
        </div>
        {html && file ? <ArtifactPreview artifact={file} liveApps={reference === primaryHtml ? liveApps : []} /> : html && !error ? <p role="status" className="text-muted-foreground flex items-center gap-2 py-4 text-xs"><LoaderCircleIcon className="size-3.5 animate-spin" />Loading app preview…</p> : null}
        {html && error && !file && reference === primaryHtml ? <LiveAppPreviews apps={liveApps} /> : null}
      </section>;
    })}
    {error ? <div role="alert" className="flex flex-wrap items-center gap-2"><p className="text-destructive text-xs">Couldn’t load the exported file: {error}</p><Button variant="outline" size="sm" onClick={() => setRevision((value) => value + 1)}>Retry</Button></div> : null}
  </div>;
}
