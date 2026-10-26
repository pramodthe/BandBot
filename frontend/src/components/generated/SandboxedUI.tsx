import { useEffect, useMemo, useRef, useState } from "react";
import { DownloadIcon, RotateCcwIcon } from "lucide-react";
import { api } from "@/api";
import type { SandboxedUISurface } from "@/types";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { downloadArtifact } from "./artifacts";
import { UI_SANDBOX, sandboxDocument, sandboxProposal, type SandboxProposal } from "./sandbox-document";

type Pending = SandboxProposal & { requestId: string };

export default function SandboxedUI({ surface, componentId, channelId, canRespond }: {
  surface: SandboxedUISurface; componentId: number; channelId: number; canRespond: boolean;
}) {
  const frame = useRef<HTMLIFrameElement>(null);
  const pendingRef = useRef<Pending | null>(null);
  const busyRef = useRef(false);
  const [revision, setRevision] = useState(0);
  const [height, setHeight] = useState(surface.initialHeight);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [status, setStatus] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const signature = JSON.stringify(surface);
  const nonce = useMemo(() => crypto.randomUUID(), [signature, revision]);
  const document = useMemo(() => sandboxDocument(surface, nonce, window.location.origin), [signature, nonce]);
  const canRespondRef = useRef(canRespond);
  canRespondRef.current = canRespond;

  const reply = (id: string, ok: boolean) => frame.current?.contentWindow?.postMessage({ kind: "band-sandbox:response", nonce, id, ok }, "*");

  useEffect(() => {
    const target = frame.current;
    const postTheme = () => {
      const styles = getComputedStyle(window.document.documentElement);
      const colors = Object.fromEntries(["background", "foreground", "muted", "muted-foreground", "primary", "primary-foreground", "border", "card", "card-foreground"].map((name) => [name, styles.getPropertyValue(`--${name}`).trim()]));
      target?.contentWindow?.postMessage({ kind: "band-sandbox:theme", nonce, colors, dark: window.document.documentElement.dataset.theme === "dark" }, "*");
    };
    const respond = (id: string, ok: boolean) => target?.contentWindow?.postMessage({ kind: "band-sandbox:response", nonce, id, ok }, "*");
    const receive = (event: MessageEvent) => {
      if (event.source !== target?.contentWindow || event.origin !== "null" || !event.data || event.data.nonce !== nonce) return;
      if (event.data.kind === "band-sandbox:ready") { setLoading(false); postTheme(); return; }
      if (event.data.kind === "band-sandbox:error") { setLoading(false); setError("This interface could not finish loading. Reload it to try again."); return; }
      if (event.data.kind === "band-sandbox:resize") {
        if (Number.isFinite(event.data.height)) setHeight(Math.max(240, Math.min(1200, Math.round(event.data.height))));
        return;
      }
      const proposal = sandboxProposal(event.data, nonce);
      if (!proposal) return;
      if (pendingRef.current || (proposal.method === "sendPrompt" && !canRespondRef.current)) { respond(proposal.id, false); return; }
      const next = { ...proposal, requestId: crypto.randomUUID() };
      pendingRef.current = next;
      setPending(next); setText(next.text ?? ""); setError(""); setStatus("");
    };
    window.addEventListener("message", receive);
    const observer = new MutationObserver(postTheme);
    observer.observe(window.document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
    return () => {
      window.removeEventListener("message", receive);
      observer.disconnect();
      pendingRef.current = null;
    };
  }, [nonce]);

  const finish = (ok: boolean) => {
    const current = pendingRef.current;
    if (current) reply(current.id, ok);
    pendingRef.current = null;
    setPending(null);
  };
  const send = async () => {
    const current = pendingRef.current;
    if (!current || current.method !== "sendPrompt" || busyRef.current || !canRespond || !text.trim()) return;
    busyRef.current = true; setBusy(true); setError("");
    try {
      await api("POST", `/api/channels/${channelId}/components/${componentId}/followup`, { request_id: current.requestId, text: text.trim() });
      finish(true); setStatus("Follow-up sent to the agent.");
    } catch (cause) { setError((cause as Error).message); }
    finally { busyRef.current = false; setBusy(false); }
  };
  const reload = () => {
    if (busyRef.current) return;
    finish(false); setLoading(true); setError(""); setStatus(""); setHeight(surface.initialHeight); setRevision((value) => value + 1);
  };
  return <section aria-label={surface.title} className="border-border bg-background flex min-w-0 flex-col overflow-hidden rounded-xl border">
    <header className="flex items-start justify-between gap-3 border-b p-4">
      <div className="min-w-0"><h3 className="font-semibold break-words">{surface.title}</h3>{surface.description && <p className="text-muted-foreground mt-1 text-sm break-words">{surface.description}</p>}</div>
      <div className="flex shrink-0 gap-1"><Button variant="ghost" size="icon-sm" disabled={busy} aria-label={`Reload ${surface.title}`} onClick={reload}><RotateCcwIcon /></Button><Button variant="ghost" size="icon-sm" aria-label={`Download ${surface.title}`} onClick={() => downloadArtifact({ path: `${surface.title.replace(/[^a-z0-9_-]+/gi, "-").slice(0, 60) || "interface"}.html`, content: sandboxDocument(surface, "standalone", "null") })}><DownloadIcon /></Button></div>
    </header>
    {loading && <p role="status" className="text-muted-foreground px-4 py-2 text-sm">{surface.placeholderMessages[0] ?? "Loading interface…"}</p>}
    <iframe ref={frame} key={nonce} title={`Interactive interface: ${surface.title}`} srcDoc={document} sandbox={UI_SANDBOX}
      referrerPolicy="no-referrer" allow="camera 'none'; microphone 'none'; geolocation 'none'; payment 'none'; clipboard-read 'none'; clipboard-write 'none'"
      onLoad={() => setLoading(false)} className="w-full border-0" style={{ height }} />
    {pending && <div className="border-border bg-muted/30 flex flex-col gap-3 border-t p-4" role="region" aria-label="Review interface request">
      {pending.method === "sendPrompt" ? <>
        <label className="flex flex-col gap-2 text-sm font-medium">Review the follow-up before sending<Textarea value={text} onChange={(event) => setText(event.target.value)} maxLength={4000} rows={4} disabled={busy} /></label>
        <p className="text-muted-foreground text-xs">Sends a message to the agent in this channel. External actions still need their usual approval.</p>
        <div className="flex flex-wrap gap-2"><Button disabled={!canRespond || busy || !text.trim()} onClick={() => void send()}>{busy ? "Sending…" : "Send to agent"}</Button><Button variant="outline" disabled={busy} onClick={() => finish(false)}>Cancel</Button></div>
      </> : <>
        <p className="text-sm">The interface proposes opening this link:</p><p className="text-muted-foreground text-sm break-all">{pending.url}</p>
        <div className="flex flex-wrap gap-2"><Button asChild><a href={pending.url} target="_blank" rel="noopener noreferrer" onClick={() => finish(true)}>Open link</a></Button><Button variant="outline" onClick={() => finish(false)}>Cancel</Button></div>
      </>}
    </div>}
    {error && <p role="alert" className="text-destructive px-4 py-3 text-sm">{error}</p>}
    {status && <p role="status" className="text-muted-foreground px-4 py-3 text-sm">{status}</p>}
  </section>;
}
