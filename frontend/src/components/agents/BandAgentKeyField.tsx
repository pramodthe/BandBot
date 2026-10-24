import { useId } from "react";
import Field from "@/components/shared/Field";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";

export default function BandAgentKeyField({ value, onChange, disabled, canRegister, register, onRegisterChange }: {
  value: string; onChange: (key: string) => void; disabled: boolean;
  canRegister: boolean; register: boolean; onRegisterChange: (value: boolean) => void;
}) {
  const id = useId();
  return <section aria-label="Band agent connection" className="space-y-3 rounded-xl border p-4">
    {!register && <>
      <Field id={`${id}-key`} label="Band agent API key">
        <Input id={`${id}-key`} type="password" autoComplete="off" required maxLength={2000}
          placeholder="band_a_…" value={value} onChange={(event) => onChange(event.target.value)}
          disabled={disabled} aria-describedby={`${id}-help`} />
      </Field>
      <p id={`${id}-help`} className="text-muted-foreground text-xs leading-relaxed">
        Create an agent in the <a className="underline" href="https://app.band.ai/agents" target="_blank" rel="noreferrer">Band dashboard</a>,
        {" "}then paste that agent’s key here. Each agent needs its own key. Your key is checked and encrypted on the server.
      </p>
      <p className="text-muted-foreground text-xs leading-relaxed">Human API access is not required. Conversations here stay in this app; model access is configured separately.</p>
    </>}
    {canRegister && <label className="flex items-start gap-2.5 text-sm">
      <Checkbox checked={register} disabled={disabled} onCheckedChange={(value) => onRegisterChange(value === true)} />
      Register a new agent with the Enterprise connection instead
    </label>}
  </section>;
}
