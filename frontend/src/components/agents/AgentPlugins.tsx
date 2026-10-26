import { useEffect, useId, useState, type FormEvent } from "react";
import { LoaderCircleIcon, PlugIcon, RefreshCwIcon } from "lucide-react";
import { api } from "@/api";
import type { Agent, PluginList } from "@/types";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Card, CardContent, CardDescription, CardFooter, CardHeader, CardTitle } from "@/components/ui/card";
import { Empty, EmptyDescription, EmptyHeader, EmptyMedia, EmptyTitle } from "@/components/ui/empty";

export default function AgentPlugins({ agent, revision, onUpdated, onConnections }: {
  agent: Agent;
  revision: number;
  onUpdated: (agent: Agent) => void;
  onConnections: () => void;
}) {
  const id = useId();
  const [data, setData] = useState<PluginList | null>(null);
  const [draft, setDraft] = useState<string[] | null>(agent.plugins);
  const [baseline, setBaseline] = useState(agent.plugins);
  const [reload, setReload] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  useEffect(() => {
    let stopped = false;
    setLoading(true);
    api<PluginList>("GET", "/api/plugins").then((value) => {
      if (!stopped) { setData(value); setError(""); }
    }).catch((cause) => { if (!stopped) setError((cause as Error).message); })
      .finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [revision, reload]);

  // Refresh clean forms after a teammate changes this agent; preserve an unsaved selection.
  useEffect(() => {
    if (JSON.stringify(draft) === JSON.stringify(baseline)) setDraft(agent.plugins);
    setBaseline(agent.plugins);
  }, [agent.plugins]);

  const connected = data?.plugins.filter((plugin) => plugin.connected) ?? [];
  const dirty = JSON.stringify(draft) !== JSON.stringify(baseline);
  const unavailable = (draft ?? []).filter((slug) => !connected.some((plugin) => plugin.slug === slug));
  const choose = (slug: string, checked: boolean) => {
    const next = new Set(draft ?? connected.map((plugin) => plugin.slug));
    if (checked) next.add(slug); else next.delete(slug);
    setDraft([...next].sort());
    setNotice("");
  };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || !dirty) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api<Agent>("PATCH", `/api/agents/${agent.id}`, { plugins: draft });
      setDraft(updated.plugins);
      setBaseline(updated.plugins);
      onUpdated(updated);
      setNotice("Plugin access saved.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm">Choose which connected apps {agent.name} can use.</p>
        <Button variant="ghost" size="sm" onClick={onConnections}><PlugIcon data-icon="inline-start" />Manage connections</Button>
      </div>
      <form onSubmit={save}>
        <Card className="gap-4 rounded-xl py-5 shadow-none">
          <CardHeader className="px-4">
            <CardTitle>App access</CardTitle>
            <CardDescription>Permissions apply to this agent. External actions still follow the channel’s approval policy.</CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5 px-4">
            <label className="flex items-start gap-3" htmlFor={`${id}-all`}>
              <Checkbox id={`${id}-all`} checked={draft === null} disabled={busy || loading} className="mt-0.5"
                onCheckedChange={(value) => { setDraft(value === true ? null : connected.map((plugin) => plugin.slug).sort()); setNotice(""); }} />
              <span className="flex flex-col gap-1"><span className="text-sm font-medium">All connected apps</span>
                <span className="text-muted-foreground text-xs leading-relaxed">Includes apps connected in the future. Turn this off to choose individual apps.</span></span>
            </label>
            {loading ? <p role="status" className="text-muted-foreground flex items-center gap-2 text-sm"><LoaderCircleIcon className="size-4 animate-spin" />Loading connected apps…</p> : null}
            {connected.length ? <fieldset disabled={busy || loading} className="flex min-w-0 flex-col gap-4">
              <legend className="sr-only">Connected apps</legend>
              {connected.map((plugin) => <label key={plugin.slug} htmlFor={`${id}-${plugin.slug}`} className="flex min-w-0 items-center gap-3">
                {plugin.logo ? <img src={plugin.logo} alt="" className="size-8 shrink-0 rounded-md" /> : <PlugIcon className="text-muted-foreground size-8 shrink-0 p-1.5" />}
                <span className="flex min-w-0 flex-1 flex-col gap-0.5"><span className="text-sm font-medium">{plugin.name}</span><span className="text-muted-foreground line-clamp-2 text-xs break-words">{plugin.description}</span></span>
                <Checkbox id={`${id}-${plugin.slug}`} checked={draft === null || draft.includes(plugin.slug)} onCheckedChange={(value) => choose(plugin.slug, value === true)} />
              </label>)}
            </fieldset> : !loading && !error ? <Empty className="items-start p-0 text-left">
              <EmptyHeader className="items-start text-left"><EmptyMedia variant="icon"><PlugIcon /></EmptyMedia><EmptyTitle>No connected apps yet</EmptyTitle><EmptyDescription>Connect an app in the workspace, then choose whether this agent can use it.</EmptyDescription></EmptyHeader>
            </Empty> : null}
            {unavailable.length ? <fieldset disabled={busy} className="flex min-w-0 flex-col gap-3">
              <legend className="text-muted-foreground mb-2 text-xs">Previously selected apps that are not connected</legend>
              {unavailable.map((slug) => <label key={slug} className="flex items-center gap-3 text-sm"><Checkbox checked onCheckedChange={(value) => choose(slug, value === true)} /><span className="min-w-0 break-words">{slug}</span></label>)}
            </fieldset> : null}
            {error ? <div role="alert" className="text-destructive flex flex-wrap items-center gap-2 text-sm">{error}<Button type="button" variant="ghost" size="sm" onClick={() => setReload((value) => value + 1)}><RefreshCwIcon data-icon="inline-start" />Retry</Button></div> : null}
            {notice ? <p role="status" className="text-muted-foreground text-sm">{notice}</p> : null}
          </CardContent>
          <CardFooter className="flex flex-wrap justify-between gap-3 px-4">
            <span className="text-muted-foreground text-xs">{draft === null ? "All current and future connected apps" : draft.length ? `${draft.length} selected app${draft.length === 1 ? "" : "s"}` : "No connected-app access"}</span>
            <div className="ml-auto flex gap-2">
              {dirty ? <Button type="button" variant="ghost" disabled={busy} onClick={() => { setDraft(baseline); setNotice(""); }}>Discard</Button> : null}
              <Button type="submit" disabled={busy || loading || !dirty}>{busy ? <LoaderCircleIcon className="animate-spin" data-icon="inline-start" /> : null}Save access</Button>
            </div>
          </CardFooter>
        </Card>
      </form>
    </div>
  );
}
