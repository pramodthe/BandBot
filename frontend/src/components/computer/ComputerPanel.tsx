import { useCallback, useEffect, useState } from "react";
import { FolderIcon, MonitorIcon, XIcon } from "lucide-react";
import { api, ApiError, getToken } from "@/api";
import type { Agent, FileEntry, LiveView } from "@/types";
import { cn, parseUtc } from "@/lib/utils";
import Avatar from "@/components/shared/Avatar";
import ActivityLog from "@/components/shared/ActivityLog";
import { ComputerUse } from "@/components/assistant-ui/elements/computer-use";
import { useComputerState } from "./useComputerState";
import ComputerLiveView from "./ComputerLiveView";
import StatusDot from "@/components/shared/StatusDot";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";

type Tab = "screen" | "live" | "files" | "activity";

const time = (iso: string) => parseUtc(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
const clock = (iso: string) => parseUtc(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" });

function formatSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function raw(method: string, path: string, body?: Blob): Promise<Response> {
  const token = getToken();
  const response = await fetch(path, { method, body, headers: token ? { Authorization: `Bearer ${token}` } : {} });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new ApiError(typeof data.detail === "string" ? data.detail : "Request failed", response.status);
  }
  return response;
}

const note = "text-muted-foreground text-xs";
const emptyBox = "flex flex-col items-center gap-1.5 rounded-lg border border-dashed border-border bg-background px-4 py-9 text-center";

export default function ComputerPanel({ taskId, computerKey, agent, working, liveBlocked, others = [], onSwitch, onClose, embedded = false, channelId }: {
  taskId: string;
  computerKey?: string;
  agent: Agent | undefined;
  working: boolean;
  liveBlocked: string | null;
  others?: { agent: Agent; working: boolean }[];
  onSwitch?: (agentId: number) => void;
  onClose?: () => void;
  embedded?: boolean;
  channelId?: number;
}) {
  const base = `/api/tasks/${taskId}/computer`;
  const name = agent?.name ?? "Agent";
  const [tab, setTab] = useState<Tab>("screen");
  const { computer, error: pollError } = useComputerState(taskId, working, true, computerKey);
  const steps = computer?.steps ?? [];
  const [live, setLive] = useState<LiveView | null>(null);
  const [files, setFiles] = useState<FileEntry[] | null>(null);
  const [path, setPath] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (computer) setLive((current) => current && { ...current, control: computer.control });
  }, [computer]);

  const run = async (label: string, work: () => Promise<void>) => {
    setBusy(label);
    try { await work(); setError(""); }
    catch (err) { setError((err as Error).message); }
    finally { setBusy(""); }
  };

  const openLive = (control: boolean) => run(control ? "control" : "live", async () => {
    setLive(await api<LiveView>("POST", `${base}/live`, { control }));
    setTab("live");
  });

  const handBack = () => run("release", async () => {
    await api("DELETE", `${base}/control`);
    setLive((view) => view && { ...view, control: null });
  });

  const stop = () => run("stop", async () => {
    await api("POST", `${base}/stop`);
    setLive(null);
  });

  const browse = useCallback((next: string) => run("files", async () => {
    setFiles(await api<FileEntry[]>("GET", `${base}/files?path=${encodeURIComponent(next)}`));
    setPath(next);
  }), [base]);

  const download = (entry: FileEntry) => run(`download-${entry.path}`, async () => {
    const response = await raw("GET", `${base}/files/download?path=${encodeURIComponent(entry.path)}`);
    const url = URL.createObjectURL(await response.blob());
    const link = document.createElement("a");
    link.href = url;
    link.download = entry.name;
    link.click();
    URL.revokeObjectURL(url);
  });

  const upload = (file: File) => run("upload", async () => {
    await raw("POST", `${base}/files?name=${encodeURIComponent(file.name)}`, file);
    await browse(path);
  });

  const selectTab = (next: Tab) => {
    setTab(next);
    if (next === "files" && files === null && computer?.configured && computer.sandbox_id) void browse("");
    // Attach automatically when running; resuming a paused computer needs an explicit click.
    if (next === "live" && !live && !liveBlocked && computer?.status === "running") void openLive(false);
  };

  const parent = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) || "/" : "";
  const control = live?.control ?? computer?.control ?? null;
  const viewOnly = !control?.mine;
  const status = computer?.status === "stopped" ? "paused" : computer?.status.replaceAll("_", " ") ?? "loading";
  const dot = computer?.status === "running" ? "online" : computer?.error ? "error" : "stopped";
  const unavailable = !computer?.configured || !computer.sandbox_id
    || ["provisioning", "starting", "pausing"].includes(computer.status);
  const liveControls = <>
    <div className="flex flex-wrap gap-2">
      <Button variant="chip" size="chip" disabled={!!busy || !!liveBlocked || unavailable} onClick={() => void openLive(false)}>
        {live ? "Reconnect" : "Connect"}
      </Button>
      {!control?.mine && <Button variant="chip" size="chip" disabled={!!busy || !!liveBlocked || unavailable} onClick={() => void openLive(true)}>Take over</Button>}
      {control && <Button variant="chip" size="chip" disabled={!!busy || !!liveBlocked} onClick={() => void handBack()}>Hand back</Button>}
    </div>
    {control && <p className={note}>{control.mine ? "You control" : `${control.user_name} controls`} this computer until {time(control.until)}.</p>}
  </>;

  return (
    <aside
      className={cn("border-border bg-muted/30 max-xl:bg-background flex min-h-0 shrink-0 flex-col text-[13px]", embedded
        ? "w-full overflow-hidden rounded-xl border"
        : "w-[min(560px,44vw)] border-l max-xl:fixed max-xl:inset-y-0 max-xl:right-0 max-xl:z-20 max-xl:w-[min(100vw,560px)] max-xl:shadow-[-4px_0_16px_rgba(0,0,0,0.2)]")}
      aria-label={computer?.shared ? "Workspace computer" : `${name}'s computer`}
    >
      <header className="border-border bg-background flex min-h-[58px] items-center gap-2.5 border-b px-4 py-3">
        <Avatar name={name} kind="agent" role={agent?.role} avatar={agent?.avatar} size={28} />
        <div className="min-w-0 flex-1">
          <div className="text-[15px] font-black">{computer?.shared ? "Workspace computer" : `${name}'s computer`}</div>
          <div className="text-muted-foreground flex items-center gap-1.5 text-xs">
            <StatusDot state={dot} />
            <span className="capitalize">{status}</span>
            {working && <span className="animate-pulse font-bold text-[#1d9b5f]">● Working</span>}
          </div>
        </div>
        {onClose ? <Button variant="ghost" size="icon-xs" title="Close computer" aria-label="Close computer" onClick={onClose}>
          <XIcon />
        </Button> : null}
      </header>

      {others.length > 0 && (
        <div className="flex flex-wrap items-center gap-1.5 px-4 pt-2">
          <span className={note}>Also in this channel:</span>
          {others.map((o) => (
            <Button key={o.agent.id} variant="chip" size="chip" onClick={() => onSwitch?.(o.agent.id)}>
              {o.agent.name}{o.working ? " ●" : ""}
            </Button>
          ))}
        </div>
      )}

      <Tabs value={tab} onValueChange={(next) => selectTab(next as Tab)} className="min-h-0 flex-1 gap-0">
        <TabsList className="mx-4 mt-2.5 max-sm:grid max-sm:w-auto max-sm:grid-cols-2 max-sm:grid-rows-2 max-sm:group-data-[orientation=horizontal]/tabs:h-[70px]">
          <TabsTrigger value="screen">Screen</TabsTrigger>
          <TabsTrigger value="live">Live desktop</TabsTrigger>
          <TabsTrigger value="files">Files</TabsTrigger>
          {channelId != null ? <TabsTrigger value="activity">Activity</TabsTrigger> : null}
        </TabsList>

        <div className="flex min-h-0 flex-1 flex-col gap-2 overflow-y-auto px-4 py-2.5">
          {(error || pollError) && <p className="text-destructive text-xs" role="alert">{error || pollError}</p>}
          {computer?.network_warning && <p className="rounded-lg border border-border bg-muted p-3 text-xs" role="alert">{computer.network_warning}</p>}
          {computer && !computer.configured && <p className="text-muted-foreground">Computer access is not configured for this workspace.</p>}
          {computer?.shared && <p className={note}>Shared files, apps, and browser logins for workspace agents. Private chats stay separate.</p>}
          {computer?.error && <p className="text-destructive text-xs">{computer.error}</p>}
          {control && (
            <p className="rounded-md border border-[#d9a400] bg-[#ffc400]/12 px-2.5 py-1.5">
              {control.mine ? "You control" : `${control.user_name} controls`} this computer until {time(control.until)}.
              {computer?.shared ? "All agents wait for clicks, typing, and commands." : "The agent waits for its clicks, typing, and commands."}
            </p>
          )}

          <TabsContent value="screen" className="flex flex-col gap-2">
            {computer?.image ? <>
              <ComputerUse className="max-w-none" url={computer.url || `${name}'s desktop`} steps={steps} activeIndex={steps.length - 1}>
                <img className="block h-auto w-full bg-[#181818]" src={computer.image} alt={`${name}'s screen`} />
              </ComputerUse>
              <p className={note}>
                {working ? "Updates as the agent works" : "Last screen the agent saw"}
                {computer.observed_at && ` · ${clock(computer.observed_at)}`}
              </p>
            </> : (
              <div className={emptyBox}>
                <MonitorIcon className="text-muted-foreground size-8 opacity-60" />
                <p>No screen yet. It appears here as soon as {name} uses its desktop.</p>
                {computer?.last_action && <p className={note}>Last action: {computer.last_action}</p>}
                <p className={note}>Commands and files don't produce screens; see the Files tab or connect live.</p>
              </div>
            )}
          </TabsContent>

          <TabsContent value="live" className="flex flex-col gap-2">
            {live ? <>
              <ComputerLiveView url={live.url} viewOnly={viewOnly} name={name} controls={liveControls} />
              <p className={note}>Link valid until {time(live.expires_at)}.
                {viewOnly ? " View only: take over to use the mouse and keyboard." : ""}</p>
            </> : (
              <div className={emptyBox}>
                <p>Watch {name}'s desktop in real time, or take over the mouse and keyboard.</p>
                <p className={note}>{unavailable ? "Ask the agent to use its computer in this chat first." : "Connect to resume a paused computer. Take over pauses the agent’s mouse, keyboard, and commands for up to 15 minutes."}</p>
              </div>
            )}
            {liveControls}
            {liveBlocked && <p className={note}>{liveBlocked} The Screen and Files tabs stay available.</p>}
          </TabsContent>

          <TabsContent value="files" className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <Button variant="chip" size="chip" disabled={!!busy || path === ""} onClick={() => void browse(parent)}>Up</Button>
              <code className="min-w-0 flex-1 truncate">{path || "home"}</code>
              <Button asChild variant="chip" size="chip" className={cn("cursor-pointer", (busy || unavailable) && "pointer-events-none opacity-50")}>
                <label aria-disabled={!!busy || unavailable}>
                  Upload
                  <input type="file" hidden disabled={!!busy || unavailable}
                    onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ""; if (file) void upload(file); }} />
                </label>
              </Button>
            </div>
            {busy === "files" && <p className="text-muted-foreground">Loading files…</p>}
            {unavailable ? <p className={note}>Files appear after the agent starts its computer.</p> : null}
            {files?.length === 0 && <p className="text-muted-foreground">This folder is empty.</p>}
            {files && files.length > 0 && (
              <ul className="border-border bg-background divide-border divide-y rounded-lg border">
                {files.map((entry) => (
                  <li key={entry.path} className="flex min-h-10 items-center gap-2 px-3 py-1.5">
                    {entry.is_dir ? (
                      <Button variant="link" size="inline" className="min-w-0 justify-start text-[13px]" onClick={() => void browse(entry.path)}>
                        <FolderIcon className="text-muted-foreground" />
                        <span className="truncate">{entry.name}</span>
                      </Button>
                    ) : (
                      <span className="min-w-0 truncate">{entry.name}</span>
                    )}
                    {!entry.is_dir && <span className="text-muted-foreground shrink-0">{formatSize(entry.size)}</span>}
                    {!entry.is_dir && (
                      <Button variant="chip" size="chip" className="ms-auto" disabled={!!busy} onClick={() => void download(entry)}>
                        Download
                      </Button>
                    )}
                  </li>
                ))}
              </ul>
            )}
            <p className={note}>Uploads go to the agent's uploads folder and are announced in the channel. Up to 25 MB.</p>
          </TabsContent>
          {channelId != null ? <TabsContent value="activity"><ActivityLog channelId={channelId} taskId={taskId} /></TabsContent> : null}
        </div>
      </Tabs>

      <footer className="border-border bg-background flex items-center justify-between gap-2 border-t px-4 py-2.5">
        <Button variant="chip" size="chip" disabled={!!busy || !!liveBlocked || unavailable} onClick={() => void stop()}>
          Stop computer
        </Button>
        {computer?.sandbox_id && (
          <a className="text-mention text-xs hover:underline" href={computer.dashboard_url} target="_blank" rel="noreferrer">
            {computer.provider === "tenki" ? "Tenki" : "E2B"} dashboard
          </a>
        )}
      </footer>
    </aside>
  );
}
