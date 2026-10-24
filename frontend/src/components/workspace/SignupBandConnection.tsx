import { useId, useState, type RefObject } from "react";
import { ArrowUpRightIcon, ChevronDownIcon, EyeIcon, EyeOffIcon, KeyRoundIcon, LockKeyholeIcon } from "lucide-react";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function SignupBandConnection({ value, onChange, disabled, error, inputRef }: {
  value: string; onChange: (value: string) => void; disabled: boolean; error: string;
  inputRef: RefObject<HTMLInputElement | null>;
}) {
  const id = useId();
  const [visible, setVisible] = useState(false);
  return <section aria-labelledby={`${id}-title`} className="bg-sidebar space-y-4 rounded-2xl border border-border p-4 sm:p-5">
    <div className="flex items-center gap-3">
      <span className="bg-background text-primary flex size-9 shrink-0 items-center justify-center rounded-xl border border-border"><KeyRoundIcon className="size-4" /></span>
      <div><h3 id={`${id}-title`} className="text-sm font-semibold">Your Band connection</h3><p className="text-muted-foreground mt-0.5 text-xs">Your own key is required to connect this app to Band.</p></div>
    </div>
    <Field id={`${id}-key`} label="Band agent API key">
      <div className="relative">
        <Input ref={inputRef} id={`${id}-key`} type={visible ? "text" : "password"} value={value} onChange={(event) => onChange(event.target.value)}
          disabled={disabled} required maxLength={2000} autoComplete="off" autoCapitalize="none" spellCheck={false}
          placeholder="band_a_…" className="bg-background h-11 rounded-xl pr-11"
          aria-invalid={!!error} aria-describedby={`${id}-hint${error ? ` ${id}-error` : ""}`} />
        <Button type="button" variant="ghost" size="icon-sm" className="absolute top-1/2 right-1.5 -translate-y-1/2"
          disabled={disabled} aria-label={visible ? "Hide Band agent API key" : "Show Band agent API key"}
          aria-pressed={visible} onClick={() => setVisible((previous) => !previous)}>
          {visible ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
        </Button>
      </div>
      <p id={`${id}-hint`} className="text-muted-foreground text-xs leading-relaxed">Your Band Agent API key begins with <code className="text-foreground">band_a_</code>.</p>
      {error && <p id={`${id}-error`} role="alert" className="text-destructive text-sm leading-relaxed">{error}</p>}
    </Field>
    <div className="space-y-2.5 border-t border-border pt-3 text-xs leading-relaxed">
      <p><span className="text-foreground font-semibold">Agent API key</span><span className="bg-brand-soft text-brand-soft-foreground ml-2 rounded-full px-2 py-0.5 text-[10px] font-medium">Use this</span><br /><span className="text-muted-foreground">Authenticates this app with Band’s Agent API, using the permissions of your key.</span></p>
      <p><span className="text-foreground font-semibold">Human API key</span><span className="text-muted-foreground ml-2">band_u_…</span><br /><span className="text-muted-foreground">An Enterprise key for managing agents and rooms as a person. Use an Agent API key for signup.</span></p>
    </div>
    <details className="group border-t border-border pt-3">
      <summary className="text-primary flex cursor-pointer list-none items-center justify-between gap-2 rounded-sm text-xs font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        How do I get an agent key?<ChevronDownIcon className="size-4 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <ol className="text-muted-foreground mt-3 list-decimal space-y-2 pl-4 text-xs leading-relaxed">
        <li>Open <a className="text-foreground underline underline-offset-2" href="https://app.band.ai/agents" target="_blank" rel="noreferrer">Agents in Band</a> and choose <strong className="text-foreground font-medium">Connect remote agent</strong>.</li>
        <li>Add a name and description, then choose <strong className="text-foreground font-medium">Connect Agent</strong>.</li>
        <li>Copy the <strong className="text-foreground font-medium">API Key</strong> from the confirmation and paste it above. Band shows the key once, so save it securely.</li>
      </ol>
      <a className="text-primary mt-3 inline-flex items-center gap-1 text-xs font-medium underline-offset-2 hover:underline" href="https://docs.band.ai/getting-started/connect-remote-agent#step-2-create-your-agent-in-band" target="_blank" rel="noreferrer">Read Band’s setup guide<ArrowUpRightIcon className="size-3" /></a>
    </details>
    <p className="text-muted-foreground flex items-start gap-1.5 text-[11px] leading-relaxed"><LockKeyholeIcon className="mt-0.5 size-3 shrink-0" />Your key is checked with Band and stored encrypted on the server.</p>
  </section>;
}
