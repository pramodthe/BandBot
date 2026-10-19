import { useEffect, useState } from "react";
import { DownloadIcon, FileTextIcon, RefreshCwIcon, Trash2Icon } from "lucide-react";
import { api, documentBlob, downloadDocument } from "@/api";
import type { WorkspaceDocument } from "@/types";
import type { State } from "@/store";
import { isAdmin } from "@/store";
import ModalShell from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Alert, AlertDescription } from "@/components/ui/alert";

const fileSize = (size: number) => size < 1024 ? `${size} B` : `${Math.ceil(size / 1024)} KB`;

export function DocumentPreview({ documentId, onClose }: { documentId: string; onClose: () => void }) {
  const [doc, setDoc] = useState<WorkspaceDocument | null>(null);
  const [error, setError] = useState("");
  const [image, setImage] = useState("");
  useEffect(() => {
    let stopped = false;
    setDoc(null);
    setError("");
    api<WorkspaceDocument>("GET", `/api/documents/${documentId}`).then((value) => { if (!stopped) setDoc(value); })
      .catch((e: Error) => { if (!stopped) setError(e.message); });
    return () => { stopped = true; };
  }, [documentId]);
  const isImage = doc?.media_type.startsWith("image/") ?? false;
  useEffect(() => {
    if (!isImage) return;
    const controller = new AbortController();
    let url = "";
    setImage("");
    documentBlob(documentId, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      url = URL.createObjectURL(blob);
      setImage(url);
    }).catch((e: Error) => { if (!controller.signal.aborted) setError(e.message); });
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url); };
  }, [documentId, isImage]);
  return <ModalShell title={doc?.name ?? "Document"} onClose={onClose} wide>
    {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
    {!doc && !error && <p role="status">Loading document…</p>}
    {doc && <div className="flex min-w-0 flex-col gap-4">
      <div className="flex items-center justify-between gap-3"><Badge variant="secondary">{doc.status}</Badge><Button variant="outline" onClick={() => downloadDocument(doc.id, doc.name).catch((e: Error) => setError(e.message))}><DownloadIcon data-icon="inline-start" />Download original</Button></div>
      {doc.error && <p className="text-destructive text-sm">{doc.error}</p>}
      {isImage ? image ? <img src={image} alt={doc.name} className="max-h-[65vh] w-full rounded-lg object-contain" /> : !error && <p role="status">Loading image…</p> : <>
        <pre className="max-h-[55vh] overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-muted p-4 font-sans text-sm">{doc.preview || "The text preview will appear after processing. You can download the original now."}</pre>
        <p className="text-muted-foreground text-xs">Preview shows up to 30,000 characters. Vision transcriptions should be checked against the original.</p>
      </>}
    </div>}
  </ModalShell>;
}

export function DocumentLink({ documentId, children }: { documentId: string; children: React.ReactNode }) {
  const [open, setOpen] = useState(false);
  return <><button type="button" className="text-mention cursor-pointer text-left underline-offset-2 hover:underline" onClick={() => setOpen(true)}>{children}</button>{open && <DocumentPreview documentId={documentId} onClose={() => setOpen(false)} />}</>;
}

export function AttachmentCard({ document }: { document: WorkspaceDocument }) {
  const [doc, setDoc] = useState(document);
  useEffect(() => {
    if (!["queued", "processing"].includes(doc.status)) return;
    let stopped = false;
    const timer = window.setTimeout(() => {
      api<WorkspaceDocument>("GET", `/api/documents/${doc.id}`).then((value) => { if (!stopped) setDoc(value); })
        .catch(() => { if (!stopped) setDoc((value) => ({ ...value, status: "deleted" })); });
    }, 2500);
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [doc]);
  return <div className="flex max-w-xs items-center gap-2 rounded-lg border bg-background p-3 text-sm">
    <FileTextIcon className="size-5 shrink-0 text-muted-foreground" />
    <div className="min-w-0"><DocumentLink documentId={doc.id}>{doc.name}</DocumentLink><p className="text-muted-foreground text-xs">{doc.status === "staged" ? "Ready to send" : doc.status} · {fileSize(doc.size)}</p>{doc.error && <p className="text-destructive text-xs">{doc.error}</p>}</div>
  </div>;
}

export default function DocumentsPanel({ channelId, state, onClose }: { channelId: number; state: State; onClose: () => void }) {
  const [rows, setRows] = useState<WorkspaceDocument[] | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const [preview, setPreview] = useState<string | null>(null);
  const [revision, setRevision] = useState(0);
  useEffect(() => {
    let stopped = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const values = await api<WorkspaceDocument[]>("GET", `/api/channels/${channelId}/documents`);
        if (!stopped) { setRows(values); setError(""); }
      } catch (e) { if (!stopped) setError((e as Error).message); }
      finally { if (!stopped) timer = window.setTimeout(load, 2500); }
    };
    void load();
    return () => { stopped = true; window.clearTimeout(timer); };
  }, [channelId, revision]);
  const action = async (doc: WorkspaceDocument, remove: boolean) => {
    setBusy(doc.id); setError("");
    try {
      await api(remove ? "DELETE" : "POST", `/api/documents/${doc.id}${remove ? "" : "/retry"}`);
      if (remove) setRows((values) => values?.filter((d) => d.id !== doc.id) ?? null);
      setRevision((v) => v + 1);
    } catch (e) { setError((e as Error).message); }
    finally { setBusy(null); }
  };
  return <>
    <ModalShell title="Files" onClose={onClose} wide>
      <div className="flex flex-col gap-4">
        <p className="text-muted-foreground text-sm">Files shared in this conversation. Attach a file to a message to add it, then ask an agent to analyze it.</p>
        {error && <Alert variant="destructive"><AlertDescription>{error}</AlertDescription></Alert>}
        {rows === null && !error && <p role="status">Loading documents…</p>}
        {rows?.length === 0 && <p className="py-8 text-center text-sm text-muted-foreground">No documents yet. Use the paperclip in chat to share your first file.</p>}
        <div className="flex max-h-[60vh] flex-col gap-2 overflow-y-auto">
          {rows?.map((doc) => <div key={doc.id} className="flex items-center gap-3 rounded-lg border p-3">
            <FileTextIcon className="size-5 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1"><button type="button" className="block max-w-full cursor-pointer truncate text-left text-sm font-medium hover:underline" onClick={() => setPreview(doc.id)}>{doc.name}</button><span className="text-xs text-muted-foreground">{fileSize(doc.size)}</span>{doc.error && <p className="text-xs text-destructive">{doc.error}</p>}</div>
            <Badge variant="secondary">{doc.status}</Badge>
            <Button size="icon-sm" variant="ghost" aria-label={`Download ${doc.name}`} onClick={() => downloadDocument(doc.id, doc.name).catch((e: Error) => setError(e.message))}><DownloadIcon /></Button>
            {(doc.uploaded_by === state.me.id || isAdmin(state)) && <>
              {doc.status === "error" && <Button size="icon-sm" variant="ghost" disabled={busy === doc.id} aria-label={`Retry ${doc.name}`} onClick={() => action(doc, false)}><RefreshCwIcon /></Button>}
              <Button size="icon-sm" variant="ghost" disabled={busy === doc.id} aria-label={`Remove ${doc.name}`} onClick={() => action(doc, true)}><Trash2Icon /></Button>
            </>}
          </div>)}
        </div>
      </div>
    </ModalShell>
    {preview && <DocumentPreview documentId={preview} onClose={() => setPreview(null)} />}
  </>;
}
