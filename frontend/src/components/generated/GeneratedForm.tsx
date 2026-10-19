import { useRef, useState, type FormEvent } from "react";
import { CheckCircle2Icon, MessageSquareReplyIcon } from "lucide-react";
import { api, ApiError } from "@/api";
import type { FormResponse, MessageComponent } from "@/types";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectGroup, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export default function GeneratedForm({ component, channelId, canRespond }: {
  component: Extract<MessageComponent, { kind: "form" }>;
  channelId: number;
  canRespond: boolean;
}) {
  const form = component.props;
  const [values, setValues] = useState<Record<string, string | boolean>>({});
  const [receipt, setReceipt] = useState<FormResponse | null>(null);
  const [sending, setSending] = useState<string | null>(null);
  const [error, setError] = useState("");
  const inFlight = useRef(false);
  const response = component.response ?? receipt;
  const disabled = sending !== null || !canRespond;
  const base = `/api/channels/${channelId}/components/${component.id}`;

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (inFlight.current || response || !canRespond) return;
    const chosen = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null;
    const action = form.actions.find((item) => item.id === chosen?.value) ?? form.actions[0];
    const clean: Record<string, string | boolean | number> = {};
    for (const field of form.fields) {
      const value = values[field.id] ?? (field.type === "checkbox" ? false : "");
      if (field.required && (value === "" || (typeof value === "string" && !value.trim()) || (field.type === "checkbox" && !value))) {
        setError(`${field.label} is required`);
        return;
      }
      if (value === "") continue;
      if (field.type === "number") {
        const number = Number(value);
        if (!Number.isFinite(number)) { setError(`${field.label} must be a number`); return; }
        clean[field.id] = number;
      } else clean[field.id] = value;
    }
    inFlight.current = true;
    setSending(action.id);
    setError("");
    try {
      const result = await api<{ response: FormResponse }>("POST", `${base}/respond`, { action_id: action.id, values: clean });
      setReceipt(result.response);
    } catch (cause) {
      setError((cause as Error).message);
      if (cause instanceof ApiError && cause.status === 409) {
        // Another teammate may have answered first; show the durable shared receipt.
        try {
          const latest = await api<{ response: FormResponse | null }>("GET", base);
          if (latest.response) { setReceipt(latest.response); setError(""); }
        } catch { /* Keep the original error and entered values so the person can retry. */ }
      }
    } finally {
      inFlight.current = false;
      setSending(null);
    }
  };

  return (
    <section aria-label={form.title} className="border-border bg-background overflow-hidden rounded-xl border">
      <div className="flex items-start gap-3 px-5 pt-5">
        <MessageSquareReplyIcon className="text-primary mt-0.5 size-5 shrink-0" />
        <div className="min-w-0 flex-1">
          <h3 className="font-semibold break-words">{form.title}</h3>
          {form.description && <p className="text-muted-foreground mt-1 whitespace-pre-wrap text-[13px] break-words">{form.description}</p>}
        </div>
      </div>
      {response ? (
        <div role="status" className="flex flex-col gap-3 p-5">
          <p className="text-primary flex items-center gap-2 text-sm"><CheckCircle2Icon className="size-4 shrink-0" /><span><strong>{response.userName}</strong> chose <strong>{response.label}</strong></span></p>
          {form.fields.some((field) => field.id in response.values) && (
            <dl className="border-border grid grid-cols-[minmax(0,1fr)_minmax(0,2fr)] gap-x-4 gap-y-2 border-t pt-3 text-[13px]">
              {form.fields.filter((field) => field.id in response.values).map((field) => (
                <div key={field.id} className="contents"><dt className="text-muted-foreground break-words">{field.label}</dt><dd className="whitespace-pre-wrap break-words">{typeof response.values[field.id] === "boolean" ? response.values[field.id] ? "Yes" : "No" : String(response.values[field.id])}</dd></div>
              ))}
            </dl>
          )}
          <p className="text-muted-foreground text-xs">Response saved in the conversation.</p>
        </div>
      ) : (
        <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4 p-5" aria-busy={sending !== null}>
          {form.fields.map((field) => {
            const id = `form-${component.id}-${field.id}`;
            const value = values[field.id];
            const update = (value: string | boolean) => setValues((current) => ({ ...current, [field.id]: value }));
            return <Field key={field.id} id={id} label={field.label} hint={field.required ? "(required)" : undefined}>
              {field.type === "textarea" ? (
                <Textarea id={id} required={field.required} maxLength={2000} rows={3} disabled={disabled} placeholder={field.placeholder} value={String(value ?? "")} onChange={(event) => update(event.target.value)} />
              ) : field.type === "select" ? (
                <Select value={typeof value === "string" ? value : ""} onValueChange={update} disabled={disabled} required={field.required}>
                  <SelectTrigger id={id} className="w-full"><SelectValue placeholder={field.placeholder || "Choose an option"} /></SelectTrigger>
                  <SelectContent><SelectGroup>{field.options.map((option) => <SelectItem key={option} value={option}>{option}</SelectItem>)}</SelectGroup></SelectContent>
                </Select>
              ) : field.type === "checkbox" ? (
                <Checkbox id={id} required={field.required} disabled={disabled} checked={value === true} onCheckedChange={(checked) => update(checked === true)} />
              ) : (
                <Input id={id} type={field.type} required={field.required} step={field.type === "number" ? "any" : undefined} maxLength={2000} disabled={disabled} placeholder={field.placeholder} value={String(value ?? "")} onChange={(event) => update(event.target.value)} />
              )}
            </Field>;
          })}
          {error && <p role="alert" className="text-destructive text-sm">{error}</p>}
          {!canRespond && <p className="text-muted-foreground text-xs">Join this channel before responding.</p>}
          <div className="flex flex-wrap gap-2">
            {form.actions.map((action) => <Button key={action.id} type="submit" name="action" value={action.id} variant={action.variant === "primary" ? "default" : "secondary"} disabled={disabled} className="h-auto min-h-9 whitespace-normal text-left">{sending === action.id ? "Sending…" : action.label}</Button>)}
          </div>
          <p className="text-muted-foreground text-xs">Your response goes to the agent. External actions may still require approval.</p>
        </form>
      )}
    </section>
  );
}
