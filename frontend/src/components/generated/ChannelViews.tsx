import { lazy, Suspense, useEffect, useState } from "react";
import { ArrowLeftIcon, ChevronRightIcon, LayoutDashboardIcon } from "lucide-react";
import { api } from "@/api";
import type { ChannelGeneratedView } from "@/types";
import { ViewBody } from "./GeneratedView";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";
const AssistantUIBody = lazy(() => import("./AssistantUI").then((module) => ({ default: module.AssistantUIBody })));
const SandboxedUI = lazy(() => import("./SandboxedUI"));

export default function ChannelViews({ channelId, open, onClose }: { channelId: number; open: boolean; onClose: () => void }) {
  const [views, setViews] = useState<ChannelGeneratedView[] | null>(null);
  const [selected, setSelected] = useState<ChannelGeneratedView | null>(null);
  const [query, setQuery] = useState(0);
  const [error, setError] = useState("");
  useEffect(() => {
    if (!open) return;
    let stopped = false;
    setSelected(null);
    setError("");
    api<ChannelGeneratedView[]>("GET", `/api/channels/${channelId}/views`).then((loaded) => {
      if (!stopped) setViews(loaded);
    }).catch((cause) => { if (!stopped) setError((cause as Error).message); });
    return () => { stopped = true; };
  }, [channelId, query, open]);
  return <Dialog open={open} onOpenChange={(next) => !next && onClose()}><DialogContent className="flex max-h-[90dvh] flex-col gap-0 overflow-hidden p-0 sm:max-w-5xl">
    <DialogHeader className="border-border border-b p-6 pr-12"><DialogTitle>{selected ? selected.props.title : "Channel views"}</DialogTitle><DialogDescription>{selected ? selected.props.description || `Created by ${selected.author_name}` : "Dashboards, reports, and trackers your AI teammates have created here."}</DialogDescription></DialogHeader>
    <div className="min-h-0 overflow-y-auto p-6">
      {selected ? <div className="flex flex-col gap-5"><div><Button variant="ghost" size="sm" onClick={() => setSelected(null)}><ArrowLeftIcon data-icon="inline-start" />All views</Button></div>{selected.kind === "view" ? <ViewBody view={selected.props} /> : <Suspense fallback={<p role="status">Loading interface…</p>}>{selected.kind === "sandbox" ? <SandboxedUI surface={selected.props} componentId={selected.id} channelId={channelId} canRespond /> : <AssistantUIBody surface={selected.props} />}</Suspense>}</div> : <div className="flex flex-col gap-3">
        {error && <div role="alert" className="flex flex-col items-start gap-2"><p className="text-destructive text-sm">{error}</p><Button variant="secondary" size="sm" onClick={() => setQuery((value) => value + 1)}>Try again</Button></div>}
        {!views && !error && <p role="status" className="text-muted-foreground text-sm">Loading views…</p>}
        {views?.length === 0 && <Empty><EmptyHeader><EmptyMedia variant="icon"><LayoutDashboardIcon /></EmptyMedia><EmptyTitle>A useful view starts with a question</EmptyTitle><EmptyDescription>Ask an agent to build a dashboard, comparison, or project tracker. Their generated views will be saved here.</EmptyDescription></EmptyHeader></Empty>}
        {views?.map((view) => <button key={view.id} type="button" className="border-border hover:bg-muted flex items-center gap-4 rounded-xl border p-4 text-left" onClick={() => setSelected(view)}><span className="bg-muted text-primary rounded-lg p-2.5"><LayoutDashboardIcon className="size-5" /></span><span className="flex min-w-0 flex-1 flex-col gap-1"><span className="font-semibold break-words">{view.props.title}</span>{view.props.description && <span className="text-muted-foreground line-clamp-2 text-sm">{view.props.description}</span>}<span className="text-muted-foreground text-xs">{view.author_name} · {new Date(view.created_at).toLocaleDateString()}</span></span><ChevronRightIcon className="text-muted-foreground size-4 shrink-0" /></button>)}
      </div>}
    </div>
  </DialogContent></Dialog>;
}
