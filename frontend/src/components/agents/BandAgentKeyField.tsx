import { useId } from "react";
import Field from "@/components/shared/Field";
import { Input } from "@/components/ui/input";

export default function BandAgentKeyField({ value, onChange, disabled }: {
  value: string; onChange: (key: string) => void; disabled: boolean;
}) {
  const id = useId();
  return <section aria-label="Band agent connection" className="space-y-3 rounded-xl border p-4">
    <Field id={`${id}-key`} label="Band agent API key">
      <Input id={`${id}-key`} type="password" autoComplete="off" required maxLength={2000}
        placeholder="band_a_…" value={value} onChange={(event) => onChange(event.target.value)}
        disabled={disabled} aria-describedby={`${id}-help`} />
    </Field>
    <p id={`${id}-help`} className="text-muted-foreground text-xs leading-relaxed">
      Create an agent in the <a className="underline" href="https://app.band.ai/agents" target="_blank" rel="noreferrer">Band dashboard</a>,
      {" "}then paste that agent’s key here. Each agent needs its own key. Your key is checked and encrypted on the server.
    </p>
    <p className="text-muted-foreground text-xs leading-relaxed">BandBot uses the Agent API. Model access is configured separately.</p>
  </section>;
}
