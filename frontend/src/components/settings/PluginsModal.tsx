import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { isAdmin, type State } from "@/store";
import type { Plugin, PluginList } from "@/types";
import Field from "@/components/shared/Field";
import ModalShell from "@/components/shared/ModalShell";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function PluginsModal({ state, onClose }: { state: State; onClose: () => void }) {
  const [query, setQuery] = useState("");
  const [list, setList] = useState<PluginList | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const request = useRef(0);
  const admin = isAdmin(state);

  const load = useCallback(async (search: string) => {
    const current = ++request.current;
    setLoading(true);
    setError("");
    try {
      const loaded = await api<PluginList>("GET", `/api/plugins?q=${encodeURIComponent(search)}`);
      if (current === request.current) setList(loaded);
    } catch (err) {
      if (current === request.current) setError((err as Error).message);
    } finally {
      if (current === request.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    setLoading(true);
    setError("");
    const timer = window.setTimeout(() => void load(query), 250);
    return () => {
      window.clearTimeout(timer);
      request.current += 1;
    };
  }, [query, load]);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") void load(query);
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [query, load]);

  const connect = async (plugin: Plugin) => {
    setBusy(plugin.slug);
    setError("");
    try {
      const { url } = await api<{ url: string }>("POST", `/api/plugins/${plugin.slug}/connect`);
      const opened = window.open(url, "_blank");
      if (!opened) setError("The browser blocked the sign-in window. Allow popups for this site, then choose Connect again.");
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  const disconnect = async (plugin: Plugin) => {
    setBusy(plugin.slug);
    setError("");
    try {
      await api("DELETE", `/api/plugins/${plugin.slug}`);
      await load(query);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <ModalShell title="Plugins" onClose={onClose} wide>
      <div className="flex flex-col gap-3.5">
        <p className="text-muted-foreground text-[13.5px]">
          Connect an app once. Every agent can use it, unless you limit that agent to specific plugins.
        </p>
        {list && !list.configured && (
          <div className="bg-muted border-border rounded-lg border px-3.5 py-3 text-sm">
            Add <strong>COMPOSIO_API_KEY</strong> to the server environment, then restart the API and the worker.
            Connect stays off until then.
          </div>
        )}
        <Field id="plugin-search" label="Search">
          <Input
            id="plugin-search"
            value={query}
            maxLength={80}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Gmail, Slack, GitHub…"
          />
        </Field>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>
              <p className="break-words">{error}</p>
              <Button type="button" variant="secondary" size="sm" onClick={() => void load(query)}>
                Try again
              </Button>
            </AlertDescription>
          </Alert>
        )}
        {loading && <p role="status" className="text-muted-foreground text-sm">Loading apps…</p>}
        <div className="flex flex-col gap-2" aria-busy={loading}>
          {(list?.plugins ?? []).map((plugin) => (
            <div key={plugin.slug} className="border-border flex items-center gap-3 rounded-lg border px-3 py-2.5">
              {plugin.logo ? (
                <img src={plugin.logo} alt="" className="size-8 rounded" />
              ) : (
                <div className="bg-muted text-muted-foreground grid size-8 place-items-center rounded text-xs font-black">
                  {plugin.name.slice(0, 1)}
                </div>
              )}
              <div className="min-w-0 flex-1">
                <div className="font-bold">{plugin.name}</div>
                {plugin.description && <div className="text-muted-foreground truncate text-[12.5px]">{plugin.description}</div>}
              </div>
              {plugin.connected ? (
                <Button
                  type="button"
                  variant="secondary"
                  size="sm"
                  disabled={!admin || busy === plugin.slug || !list?.configured}
                  onClick={() => void disconnect(plugin)}
                >
                  {busy === plugin.slug ? "Disconnecting…" : "Disconnect"}
                </Button>
              ) : (
                <Button
                  type="button"
                  size="sm"
                  disabled={!admin || busy === plugin.slug || !list?.configured}
                  onClick={() => void connect(plugin)}
                >
                  {busy === plugin.slug ? "Opening…" : "Connect"}
                </Button>
              )}
            </div>
          ))}
          {!loading && !error && list && list.plugins.length === 0 && (
            <div className="text-muted-foreground text-sm">No apps match that search.</div>
          )}
        </div>
        {!admin && <p className="text-muted-foreground text-[12.5px]">Only admins can connect or disconnect plugins.</p>}
      </div>
    </ModalShell>
  );
}
