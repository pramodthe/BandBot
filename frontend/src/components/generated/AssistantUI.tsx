import { createContext, useContext, useEffect, useRef, useState } from "react";
import { CheckCircle2Icon, MessageSquareReplyIcon } from "lucide-react";
import { defaultGenerativeUILibrary, renderGenerativeUI, type GenerativeUIAction, type GenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import { api, ApiError } from "@/api";
import type { AssistantUISurface, InputReceipt, InputRequest, MessageComponent } from "@/types";
import { styledGenerativeUILibrary } from "@/components/assistant-ui/elements/generative-ui";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import Field from "@/components/shared/Field";
import RichText from "@/components/shared/RichText";

const ResponseContext = createContext({ sending: false, id: 0 });
const BaseForm = defaultGenerativeUILibrary.Form!.render;
const responseLibrary: GenerativeUILibrary = {
  ...styledGenerativeUILibrary,
  Form: {
    ...styledGenerativeUILibrary.Form!,
    render: function ResponseForm(props) {
      const { sending, id } = useContext(ResponseContext);
      return <BaseForm {...props}>
        {props.children}
        <div className="border-border mt-2 flex flex-col gap-3 border-t pt-4">
          <Field id={`steering-${id}`} label="Anything else the agent should know?" hint="(optional)">
            <Textarea id={`steering-${id}`} name="__steering" maxLength={2000} rows={2} placeholder="Adjust the direction, add a constraint, or explain your choice…" />
          </Field>
          <div><Button type="submit">{sending ? "Saving…" : "Continue"}</Button></div>
          <p className="text-muted-foreground text-xs">Your answer continues the agent’s saved work. External actions still use the channel’s approval rules.</p>
        </div>
      </BaseForm>;
    },
  },
};

export function AssistantUIBody({ surface }: { surface: AssistantUISurface }) {
  return <div className="generated-ui min-w-0" data-aui="root">{renderGenerativeUI(surface.ui, styledGenerativeUILibrary)}</div>;
}

function fieldsOf(node: unknown): Record<string, string> {
  if (!node || typeof node !== "object" || Array.isArray(node)) return {};
  const item = node as Record<string, unknown>;
  const found: Record<string, string> = typeof item.name === "string" ? { [item.name]: String(item.label ?? item.name) } : {};
  const children = Array.isArray(item.children) ? item.children : [item.children];
  return Object.assign(found, ...children.map(fieldsOf));
}

const displayValue = (value: unknown): string => typeof value === "boolean" ? value ? "Yes" : "No" : Array.isArray(value) ? value.join(", ") : String(value ?? "");

export default function AssistantUI({ component, channelId, canRespond }: {
  component: Extract<MessageComponent, { kind: "ui" | "input" }>;
  channelId: number;
  canRespond: boolean;
}) {
  const [receipt, setReceipt] = useState<InputReceipt | null>(null);
  const [request, setRequest] = useState<InputRequest | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const input = component.kind === "input";
  const response = input ? component.response ?? receipt : null;
  const status = request ?? (input ? component.request : null);
  const remoteStatus = input ? component.request.status : null;
  useEffect(() => setRequest(null), [remoteStatus]);
  const closed = status?.status === "closed";
  const surface = component.props;
  const labels = fieldsOf(surface.ui);
  const base = `/api/channels/${channelId}/components/${component.id}`;
  const refresh = async () => {
    const latest = await api<{ response: InputReceipt | null; request: InputRequest }>("GET", base);
    setReceipt(latest.response);
    setRequest(latest.request);
  };
  const respond = async (action: GenerativeUIAction) => {
    if (action.type !== "respond" || inFlight.current || !canRespond || closed || response) return;
    const values = { ...(action.$input as Record<string, unknown> ?? {}) };
    const steering = String(values.__steering ?? "");
    delete values.__steering;
    inFlight.current = true;
    setSending(true);
    setError("");
    try {
      const result = await api<{ response: InputReceipt }>("POST", `${base}/answer`, { values, steering });
      setReceipt(result.response);
    } catch (cause) {
      setError((cause as Error).message);
      if (cause instanceof ApiError && cause.status === 409) {
        try { await refresh(); } catch { /* Retain inputs and show the original error. */ }
      }
    } finally {
      inFlight.current = false;
      setSending(false);
    }
  };
  const cancel = async () => {
    if (!status || inFlight.current) return;
    inFlight.current = true;
    setSending(true);
    try {
      await api("POST", `/api/tasks/${status.taskId}/cancel`);
      await refresh();
      setError("");
    } catch (cause) { setError((cause as Error).message); }
    finally { inFlight.current = false; setSending(false); }
  };
  return <section aria-label={surface.title} className="border-border bg-background min-w-0 overflow-hidden rounded-xl border p-5">
    <div className="mb-4 flex items-start gap-3">
      {input && <MessageSquareReplyIcon className="text-primary mt-0.5 size-5 shrink-0" />}
      <div className="min-w-0"><h3 className="font-semibold break-words">{surface.title}</h3>{surface.description && <div className="text-muted-foreground mt-1 text-[13px] break-words"><RichText text={surface.description} /></div>}</div>
    </div>
    {response ? <div role="status" className="flex flex-col gap-3">
      <p className="text-primary flex items-center gap-2 text-sm"><CheckCircle2Icon className="size-4 shrink-0" />{response.userName} responded</p>
      <dl className="grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-4 gap-y-2 text-sm">{Object.entries(response.values.answers).map(([name, value]) => <div key={name} className="contents"><dt className="text-muted-foreground break-words">{labels[name] ?? name}</dt><dd className="whitespace-pre-wrap break-words">{displayValue(value)}</dd></div>)}</dl>
      {response.values.steering && <p className="text-sm whitespace-pre-wrap break-words"><strong>Additional direction:</strong> {response.values.steering}</p>}
      <p className="text-muted-foreground text-xs">Answer saved with the task.</p>
    </div> : input ? <>
      {closed ? <p role="status" className="text-muted-foreground text-sm">This task is paused or cancelled. Resume it from its task menu to answer.</p> : <>
        {!!surface.required?.length && <p className="text-muted-foreground mb-3 text-xs">Required: {surface.required.map((name) => labels[name] ?? name).join(", ")}</p>}
        <ResponseContext.Provider value={{ sending, id: component.id }}>
          <fieldset disabled={sending || !canRespond} className="min-w-0" aria-busy={sending}>
            <div className="generated-ui min-w-0" data-aui="root">{renderGenerativeUI({ _type: "Form", gap: 4, _action: { type: "respond" }, children: surface.ui }, responseLibrary, { status: "done", dispatch: respond })}</div>
          </fieldset>
        </ResponseContext.Provider>
        {!canRespond && <p className="text-muted-foreground mt-2 text-xs">Join this channel to answer.</p>}
        {canRespond && <Button variant="ghost" size="sm" disabled={sending} onClick={() => void cancel()} className="mt-2">Cancel task</Button>}
      </>}
    </> : <AssistantUIBody surface={surface} />}
    {error && <p role="alert" className="text-destructive mt-3 text-sm">{error}</p>}
  </section>;
}
