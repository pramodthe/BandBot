import { useEffect, useState } from "react";
import { DownloadIcon, ExpandIcon } from "lucide-react";
import { documentBlob, downloadDocument } from "@/api";
import { DocumentPreview } from "@/components/documents/DocumentsPanel";
import { Button } from "@/components/ui/button";

export default function GeneratedImage({ documentId, title }: { documentId: string; title: string; model: string }) {
  const [image, setImage] = useState("");
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    const controller = new AbortController();
    let url = "";
    setImage("");
    setError("");
    documentBlob(documentId, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      if (blob.type !== "image/png") throw new Error("The generated PNG is unavailable.");
      url = URL.createObjectURL(blob);
      setImage(url);
    }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [documentId]);
  return <figure className="my-2 w-full max-w-lg overflow-hidden rounded-xl border bg-background" aria-label={title}>
    {image && <button type="button" className="block w-full cursor-zoom-in" aria-label={`Expand ${title}`} onClick={() => setExpanded(true)}>
      <img src={image} alt={title} className="max-h-[520px] w-full object-contain" />
    </button>}
    {!image && !error && <p role="status" className="p-6 text-sm text-muted-foreground">Loading generated image…</p>}
    {error && <p role="alert" className="p-4 text-sm text-destructive">{error}</p>}
    <figcaption className="flex flex-wrap items-center gap-2 border-t px-3 py-2">
      <span className="min-w-0 flex-1 text-sm font-medium">{title}</span>
      <Button variant="ghost" size="icon-sm" aria-label={`Expand ${title}`} onClick={() => setExpanded(true)}><ExpandIcon /></Button>
      <Button variant="ghost" size="sm" onClick={() => downloadDocument(documentId).catch((e: Error) => setError(e.message))}><DownloadIcon />Download PNG</Button>
    </figcaption>
    {expanded && <DocumentPreview documentId={documentId} onClose={() => setExpanded(false)} />}
  </figure>;
}
