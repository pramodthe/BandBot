import { useEffect, useId, useRef, useState } from "react";
import { CheckIcon, ExternalLinkIcon, LoaderCircleIcon, PlusIcon, UnplugIcon } from "lucide-react";
import { api } from "@/api";
import type { AgentConnection, AgentConnectionsResponse, ConnectedAgentRuntime } from "@/types";
import { connectionLabels, runtimeLabels } from "./runtime-options";
import { connectionResponse } from "./connection-response";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

export default function AgentConnectionPicker({ provider, value, onChange, disabled }: {
  provider: ConnectedAgentRuntime; value: AgentConnection | null;
  onChange: (connection: AgentConnection | null) => void; disabled: boolean;
}) {
  const id = useId();
  const [data, setData] = useState<AgentConnectionsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [reload, setReload] = useState(0);
  const [adding, setAdding] = useState(false);
  const [key, setKey] = useState("");
  const [label, setLabel] = useState(`${connectionLabels[provider]} connection`);
  const [gatewayUrl, setGatewayUrl] = useState("");
  const [gatewayAgentId, setGatewayAgentId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState<{ attempt_id: string; url: string; path: string } | null>(null);
  const [useApiKey, setUseApiKey] = useState(false);
  const mounted = useRef(false);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let stopped = false;
    setLoading(true); setError("");
    void api<unknown>("GET", "/api/agent-connections").then(connectionResponse).then((response) => {
      if (!stopped) {
        setData(response); setAdding(!response.connections.some((c) => c.provider === provider));
        setGatewayUrl(response.openclaw_gateways?.[0] ?? "");
      }
    }).catch((cause) => { if (!stopped) setError((cause as Error).message); })
      .finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [provider, reload]);

  useEffect(() => {
    if (!attempt) return;
    let stopped = false;
    let timer: number | undefined;
    const poll = async () => {
      try {
        const status = await api<{ status: string; connection_id: string | null }>("GET", `${attempt.path}/${attempt.attempt_id}`);
        if (stopped) return;
        if (status.status === "connected") {
          const response = connectionResponse(await api<unknown>("GET", "/api/agent-connections"));
          if (stopped) return;
          setData(response); onChange(response.connections.find((c) => c.id === status.connection_id) ?? null);
          setAttempt(null); setAdding(false); setError("");
        } else if (["failed", "expired", "cancelled"].includes(status.status)) {
          setAttempt(null); setError("Sign-in did not complete. Try again or connect an API key.");
        } else timer = window.setTimeout(poll, 2000);
      } catch (cause) {
        if (!stopped) { setError((cause as Error).message); timer = window.setTimeout(poll, 4000); }
      }
    };
    void poll();
    return () => {
      stopped = true; window.clearTimeout(timer);
      void api("DELETE", `${attempt.path}/${attempt.attempt_id}`).catch(() => {});
    };
  }, [attempt, onChange]);

  const connectKey = async () => {
    if (busy || disabled || !key.trim() || !label.trim() || (provider === "openclaw" && (!gatewayUrl || !gatewayAgentId.trim()))) return;
    setBusy(true); setError("");
    try {
      const connection = await api<AgentConnection>("POST", "/api/agent-connections", { provider, label: label.trim(), api_key: key,
        ...(provider === "openclaw" ? { gateway_url: gatewayUrl, gateway_agent_id: gatewayAgentId.trim() } : {}) });
      setKey(""); setData((previous) => previous ? { ...previous, connections: [...previous.connections, connection] } : previous);
      onChange(connection); setAdding(false);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  const signIn = async () => {
    const popup = window.open("about:blank", "band-codex-sign-in", "popup,width=600,height=750");
    setBusy(true); setError("");
    try {
      const path = data?.codex_local_sign_in ? "/api/agent-connections/codex/login" : "/api/agent-connections/codex/sign-in";
      const next = await api<{ attempt_id: string; url: string }>("POST", path);
      if (!mounted.current) {
        popup?.close();
        await api("DELETE", `${path}/${next.attempt_id}`);
        return;
      }
      setAttempt({ ...next, path });
      if (popup) { popup.opener = null; popup.location.href = next.url; }
    } catch (cause) { popup?.close(); setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  const disconnect = async () => {
    if (!value) return;
    setBusy(true); setError("");
    try {
      await api("DELETE", `/api/agent-connections/${value.id}`);
      setData((previous) => previous ? { ...previous, connections: previous.connections.filter((c) => c.id !== value.id) } : previous);
      onChange(null); setAdding(true);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  const connections = data?.connections.filter((connection) => connection.provider === provider) ?? [];
  return <section aria-label={`${runtimeLabels[provider]} connection`} className="border-border space-y-3 rounded-xl border p-4"
    onKeyDown={(event) => { if (adding && event.key === "Enter" && event.target instanceof HTMLInputElement) { event.preventDefault(); void connectKey(); } }}>
    <div className="flex items-center justify-between gap-2"><h3 className="text-sm font-semibold">Account connection</h3>
      {value && <span className="text-muted-foreground flex items-center gap-1 text-xs"><CheckIcon className="size-3.5" />Connected</span>}</div>
    {loading ? <p role="status" className="text-muted-foreground flex items-center gap-2 text-sm"><LoaderCircleIcon className="size-4 animate-spin" />Loading connections…</p> : null}
    {connections.length > 0 && <Select value={value?.id ?? ""} disabled={busy || disabled || !!attempt} onValueChange={(next) => { onChange(connections.find((c) => c.id === next) ?? null); setAdding(false); }}>
      <SelectTrigger className="w-full" aria-label="Connected account"><SelectValue placeholder="Choose an account" /></SelectTrigger>
      <SelectContent>{connections.map((connection) => <SelectItem key={connection.id} value={connection.id}>{connection.label}</SelectItem>)}</SelectContent>
    </Select>}
    {attempt ? <div className="space-y-2 text-sm"><p role="status">Finish signing in with ChatGPT in the opened window.</p>
      <a className="text-primary inline-flex items-center gap-1 underline" href={attempt.url} target="_blank" rel="noreferrer">Open sign-in <ExternalLinkIcon className="size-3.5" /></a>
      <Button type="button" variant="ghost" size="sm" onClick={() => setAttempt(null)}>Cancel sign-in</Button></div> : null}
    {!attempt && adding && !loading && <div className="space-y-3">
      {provider === "codex" && (data?.codex_local_sign_in || data?.codex_chatgpt_sign_in) && <>
        <Button type="button" variant="outline" className="w-full" disabled={busy || disabled} onClick={() => void signIn()}>Sign in with ChatGPT <ExternalLinkIcon className="size-4" /></Button>
        <p className="text-muted-foreground text-xs leading-relaxed">Authorize Codex in your browser. This saves a separate Codex session for workspace agents and uses your ChatGPT plan.</p>
        <Button type="button" variant="link" className="h-auto px-0 text-xs" onClick={() => setUseApiKey(!useApiKey)}>{useApiKey ? "Hide API key option" : "Use an OpenAI API key instead"}</Button>
      </>}
      {(provider !== "codex" || !(data?.codex_local_sign_in || data?.codex_chatgpt_sign_in) || useApiKey) && <>
      <Field id={`${id}-label`} label="Connection name"><Input id={`${id}-label`} maxLength={80} value={label} disabled={busy || disabled} onChange={(event) => setLabel(event.target.value)} /></Field>
      {provider === "openclaw" && <>
        <Field id={`${id}-gateway`} label="Gateway">
          <Select value={gatewayUrl} onValueChange={setGatewayUrl} disabled={busy || disabled || !data?.openclaw_gateways?.length}>
            <SelectTrigger id={`${id}-gateway`} className="w-full"><SelectValue placeholder="Choose an approved Gateway" /></SelectTrigger>
            <SelectContent>{data?.openclaw_gateways?.map((url) => <SelectItem key={url} value={url}>{url}</SelectItem>)}</SelectContent>
          </Select>
        </Field>
        <Field id={`${id}-gateway-agent`} label="Gateway agent ID">
          <Input id={`${id}-gateway-agent`} maxLength={80} value={gatewayAgentId} disabled={busy || disabled} onChange={(event) => setGatewayAgentId(event.target.value)} placeholder="e.g. band" />
        </Field>
        <p className="text-muted-foreground text-xs leading-relaxed">Use an agent configured for this workspace. The connection checks that its actions stay under the workspace's control.</p>
      </>}
      <Field id={`${id}-key`} label={provider === "openclaw" ? "Gateway token or password" : `${connectionLabels[provider]} API key`}>
        <Input id={`${id}-key`} type="password" autoComplete="off" maxLength={2000} value={key} disabled={busy || disabled} onChange={(event) => setKey(event.target.value)} placeholder={provider === "openclaw" ? "Paste your Gateway token" : "Paste your API key"} />
      </Field>
      <p className="text-muted-foreground text-xs leading-relaxed">Available to workspace agents. Credentials are encrypted on the server; usage is billed to this account.{provider === "claude" ? " Claude subscription sign-in is not available for this integration." : ""}</p>
      {provider === "cursor" && <a href="https://cursor.com/dashboard" target="_blank" rel="noreferrer" className="text-primary inline-flex items-center gap-1 text-xs underline">Get a Cursor API key <ExternalLinkIcon className="size-3" /></a>}
      <Button type="button" size="sm" disabled={busy || disabled || !key.trim() || !label.trim() || (provider === "openclaw" && (!gatewayUrl || !gatewayAgentId.trim()))} onClick={() => void connectKey()}>{busy && <LoaderCircleIcon className="size-4 animate-spin" />}{provider === "openclaw" ? "Connect Gateway" : "Connect account"}</Button>
      </>}
    </div>}
    {!loading && !attempt && <div className="flex flex-wrap gap-2">
      {connections.length > 0 && <Button type="button" variant="ghost" size="sm" disabled={busy || disabled} onClick={() => { setAdding(!adding); if (!adding) onChange(null); }}><PlusIcon />{adding ? "Use existing account" : "Connect another account"}</Button>}
      {value && !adding && <Button type="button" variant="ghost" size="sm" disabled={busy || disabled} onClick={() => void disconnect()}><UnplugIcon />Disconnect</Button>}
    </div>}
    {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
    {!loading && !data && <Button type="button" variant="outline" size="sm" onClick={() => setReload((previous) => previous + 1)}>Retry connection setup</Button>}
    {data && !data.runtimes[provider] && <p role="status" className="text-muted-foreground text-xs">{provider === "openclaw"
      ? "A server admin needs to approve a Gateway before it can be connected."
      : `An admin needs to install the ${provider === "codex" ? "Codex CLI" : provider === "cursor" ? "Cursor Agent SDK" : "Claude Agent SDK"} on the server before this agent can run.`}</p>}
  </section>;
}
