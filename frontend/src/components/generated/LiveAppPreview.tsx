import { useEffect, useId, useLayoutEffect, useRef, useState } from "react";
import { AppWindowIcon, LoaderCircleIcon, Minimize2Icon, MonitorIcon, PlugZapIcon, PowerIcon, SmartphoneIcon } from "lucide-react";
import { api } from "@/api";
import type { AppPreviewSession, LiveApp } from "@/types";
import { cn, parseUtc } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { WebPreview } from "@/components/assistant-ui/elements/web-preview";

export default function LiveAppPreview({ previewId, title, hasSavedPreview = false }: {
  previewId: string;
  title: string;
  hasSavedPreview?: boolean;
}) {
  const titleId = useId();
  const [app, setApp] = useState<LiveApp | null>(null);
  const [session, setSession] = useState<AppPreviewSession | null>(null);
  const activeSession = useRef<AppPreviewSession | null>(null);
  const mounted = useRef(true);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [device, setDevice] = useState("desktop");
  const [revision, setRevision] = useState(0);
  const frame = useRef<HTMLIFrameElement>(null);
  const surface = useRef<HTMLDialogElement>(null);

  const release = (value: AppPreviewSession | null) => {
    if (value) void api("DELETE", `/api/app-preview-sessions/${value.id}`).catch(() => {});
  };
  // Releases the server-side session and forgets it locally.
  const endSession = () => {
    release(activeSession.current);
    activeSession.current = null;
    setSession(null);
  };
  const disconnect = () => {
    endSession();
    setExpanded(false);
  };

  useEffect(() => {
    mounted.current = true;
    let stopped = false;
    let timer: number | undefined;
    const load = async () => {
      try {
        const value = await api<LiveApp>("GET", `/api/app-previews/${previewId}`);
        if (stopped) return;
        setApp(value);
        if (activeSession.current && (value.status === "paused" || value.status === "unavailable")) {
          endSession();
          setError(value.status === "paused"
            ? "The computer is paused. Resume it to reconnect."
            : "The computer has changed or is unavailable. Ask the agent to publish the app again.");
        }
      } catch (cause) {
        if (!stopped) {
          setError((cause as Error).message);
          endSession();
        }
      } finally {
        if (!stopped) timer = window.setTimeout(load, 5000);
      }
    };
    void load();
    return () => {
      stopped = true;
      mounted.current = false;
      window.clearTimeout(timer);
      release(activeSession.current);
      activeSession.current = null;
    };
  }, [previewId]);

  useEffect(() => {
    if (!session) return;
    const current = session;
    const expiry = window.setTimeout(() => {
      setError("Preview session expired. Reconnect to continue.");
      endSession();
    }, Math.max(0, parseUtc(session.expires_at).getTime() - Date.now()));
    const heartbeat = window.setInterval(() => {
      void api<{ expires_at: string }>("POST", `/api/app-preview-sessions/${current.id}/renew`, {}).then((value) => {
        if (activeSession.current?.id !== current.id) return;
        const renewed = { ...current, expires_at: value.expires_at };
        activeSession.current = renewed;
        setSession(renewed);
      }).catch((cause: Error) => {
        if (activeSession.current?.id === current.id) {
          setError(cause.message);
          endSession();
        }
      });
    }, 5 * 60 * 1000);
    const receive = (event: MessageEvent) => {
      if (event.origin !== new URL(current.url).origin || event.source !== frame.current?.contentWindow
        || event.data?.type !== "band-app-preview-error") return;
      setLoading(false);
      setError(event.data.status === 502
        ? "The app server is not responding. Check that it is running, then reload."
        : "The preview is paused, expired, or no longer available. Reconnect from chat.");
    };
    window.addEventListener("message", receive);
    return () => {
      window.clearTimeout(expiry);
      window.clearInterval(heartbeat);
      window.removeEventListener("message", receive);
    };
  }, [session]);

  // Native dialog promotion keeps this same iframe alive when expanding/collapsing.
  // Moving it into a portal would reload the app and discard unsaved client state.
  useLayoutEffect(() => {
    const dialog = surface.current;
    if (!dialog || !session) return;
    if (dialog.open) dialog.close();
    if (expanded) dialog.showModal(); else dialog.show();
    return () => { if (dialog.open) dialog.close(); };
  }, [!!session, expanded]);

  const connect = async () => {
    setBusy(true);
    setError("");
    try {
      const next = await api<AppPreviewSession>("POST", `/api/app-previews/${previewId}/sessions`, {});
      if (!mounted.current) {
        release(next);
        return;
      }
      const url = new URL(next.url);
      if (!["http:", "https:"].includes(url.protocol) || url.origin === location.origin) {
        release(next);
        throw new Error("Live previews need a separate app origin.");
      }
      release(activeSession.current);
      activeSession.current = next;
      setSession(next);
      setLoading(true);
      setRevision((value) => value + 1);
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!session) {
    return (
      <section aria-label={`Live app: ${title}`} className="border-border flex flex-col gap-3 rounded-xl border p-4">
        <div className="flex flex-wrap items-center gap-2">
          <AppWindowIcon className="text-muted-foreground size-4" />
          <h3 className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h3>
          <Badge variant="secondary">{app?.status === "paused" ? "Server paused" : app?.status === "unavailable" ? "Unavailable" : "Live app"}</Badge>
        </div>
        <p className="text-muted-foreground text-xs">
          {hasSavedPreview
            ? "Use the running app for server features. The Preview tab shows the saved webpage without starting the server."
            : "Open the running app to use it here. Disconnect when finished."}
        </p>
        {error ? <p role="alert" className="text-destructive text-xs">{error}</p> : null}
        <Button
          variant="outline"
          size="sm"
          className="self-start"
          disabled={busy || !app || app.status === "unavailable"}
          onClick={() => void connect()}
        >
          {busy ? <LoaderCircleIcon className="animate-spin" /> : <PlugZapIcon />}
          {busy ? "Connecting…" : "Open live preview"}
        </Button>
      </section>
    );
  }

  return (
    <dialog
      ref={surface}
      aria-labelledby={titleId}
      aria-modal={expanded || undefined}
      role={expanded ? "dialog" : "region"}
      onCancel={(event) => { event.preventDefault(); setExpanded(false); }}
      className={cn(
        "bg-background text-foreground border-border m-0 flex min-w-0 flex-col gap-3 rounded-2xl border p-3 shadow-xl backdrop:bg-black/65",
        expanded
          ? "fixed top-1/2 left-1/2 h-[90dvh] max-h-none w-[calc(100vw-1.5rem)] max-w-[1400px] -translate-x-1/2 -translate-y-1/2"
          : "static h-[460px] w-full max-w-none",
      )}
    >
      <div className="flex shrink-0 items-center gap-2">
        <h3 id={titleId} className="min-w-0 flex-1 truncate text-sm font-medium">{title}</h3>
        <Badge variant="secondary">{error ? "Unavailable" : "Live"}</Badge>
        {expanded ? (
          <Button variant="ghost" size="icon-sm" aria-label="Collapse app preview" onClick={() => setExpanded(false)}><Minimize2Icon /></Button>
        ) : null}
        <Button variant="ghost" size="icon-sm" aria-label="Disconnect live app" onClick={disconnect}><PowerIcon /></Button>
      </div>
      <ToggleGroup
        type="single"
        value={device}
        onValueChange={(value) => value && setDevice(value)}
        variant="outline"
        size="sm"
        aria-label="Live app screen size"
      >
        <ToggleGroupItem value="desktop" aria-label="Live desktop preview"><MonitorIcon />Desktop</ToggleGroupItem>
        <ToggleGroupItem value="phone" aria-label="Live phone preview"><SmartphoneIcon />Phone</ToggleGroupItem>
      </ToggleGroup>
      {error ? (
        <div role="alert" className="flex shrink-0 flex-wrap items-center gap-2">
          <p className="text-destructive flex-1 text-xs">{error}</p>
          <Button variant="outline" size="sm" disabled={busy} onClick={() => void connect()}>Reconnect</Button>
        </div>
      ) : null}
      <WebPreview
        origin={title}
        loading={loading}
        className="min-h-0 max-w-none flex-1"
        openLabel="Expand live app preview"
        onOpenExternal={expanded ? undefined : () => setExpanded(true)}
        onReload={() => { setError(""); setLoading(true); setRevision((value) => value + 1); }}
      >
        <div className="bg-muted flex h-full justify-center overflow-hidden">
          <iframe
            ref={frame}
            key={`${session.id}-${revision}`}
            title={`Live app: ${title}`}
            src={session.url}
            sandbox="allow-scripts allow-same-origin allow-forms allow-downloads"
            referrerPolicy="no-referrer"
            onLoad={() => setLoading(false)}
            className={cn("h-full w-full border-0 bg-white", device === "phone" && "max-w-[390px]")}
          />
        </div>
      </WebPreview>
    </dialog>
  );
}
