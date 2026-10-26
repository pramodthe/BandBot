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
      <div><h3 id={`${id}-title`} className="text-sm font-semibold">Connect Band (optional)</h3><p className="text-muted-foreground mt-0.5 text-xs">Use your own key to create and manage your agents. You can connect later in People.</p></div>
    </div>
    <Field id={`${id}-key`} label="Band user API key">
      <div className="relative">
        <Input ref={inputRef} id={`${id}-key`} type={visible ? "text" : "password"} value={value} onChange={(event) => onChange(event.target.value)}
          disabled={disabled} maxLength={2000} autoComplete="off" autoCapitalize="none" spellCheck={false}
          placeholder="band_u_…" className="bg-background h-11 rounded-xl pr-11"
          aria-invalid={!!error} aria-describedby={`${id}-hint${error ? ` ${id}-error` : ""}`} />
        <Button type="button" variant="ghost" size="icon-sm" className="absolute top-1/2 right-1.5 -translate-y-1/2"
          disabled={disabled} aria-label={visible ? "Hide Band user API key" : "Show Band user API key"}
          aria-pressed={visible} onClick={() => setVisible((previous) => !previous)}>
          {visible ? <EyeOffIcon className="size-4" /> : <EyeIcon className="size-4" />}
        </Button>
      </div>
      <p id={`${id}-hint`} className="text-muted-foreground text-xs leading-relaxed">A Band user key begins with <code className="text-foreground">band_u_</code>. Leave this blank to start chatting without Band.</p>
      {error && <p id={`${id}-error`} role="alert" className="text-destructive text-sm leading-relaxed">{error}</p>}
    </Field>
    <div className="space-y-2.5 border-t border-border pt-3 text-xs leading-relaxed">
      <p className="text-muted-foreground">Your login, channels, and conversations are managed in BandBot. Your Band key is used only for your agent management.</p>
      <p className="text-muted-foreground">Already have an agent key (<code>band_a_</code>)? You can connect that existing identity instead. Creating new Band identities requires your user key.</p>
    </div>
    <details className="group border-t border-border pt-3">
      <summary className="text-primary flex cursor-pointer list-none items-center justify-between gap-2 rounded-sm text-xs font-semibold outline-none focus-visible:ring-2 focus-visible:ring-ring [&::-webkit-details-marker]:hidden">
        How do I get a Band user key?<ChevronDownIcon className="size-4 shrink-0 transition-transform group-open:rotate-180" />
      </summary>
      <ol className="text-muted-foreground mt-3 list-decimal space-y-2 pl-4 text-xs leading-relaxed">
        <li>Open <a className="text-foreground underline underline-offset-2" href="https://app.band.ai/users/settings" target="_blank" rel="noreferrer">Band Settings</a> and go to <strong className="text-foreground font-medium">API Keys</strong>.</li>
        <li>Create a user API key and copy it securely. Band shows the key once.</li>
        <li>Paste the key above, or add it later in People. Band must allow agent management for your account.</li>
      </ol>
      <a className="text-primary mt-3 inline-flex items-center gap-1 text-xs font-medium underline-offset-2 hover:underline" href="https://docs.band.ai/api/introduction" target="_blank" rel="noreferrer">Read about Band API keys<ArrowUpRightIcon className="size-3" /></a>
    </details>
    <p className="text-muted-foreground flex items-start gap-1.5 text-[11px] leading-relaxed"><LockKeyholeIcon className="mt-0.5 size-3 shrink-0" />Your key is checked with Band and stored encrypted on the server.</p>
  </section>;
}
