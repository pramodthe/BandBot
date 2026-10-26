import type { ServiceBinding } from "@/types";
import { bindingKey, SERVICES, selectClass, type ServiceChoice } from "./provider-settings";

/** Picks a verified connection for one service. `inherit` offers following the workspace default. */
export default function ServiceSelect({ service, value, choices, onChange, inherit = false }: {
  service: string;
  value: ServiceBinding | undefined;
  choices: ServiceChoice[];
  onChange: (binding: ServiceBinding) => void;
  inherit?: boolean;
}) {
  const current = bindingKey(value);
  return (
    <select
      aria-label={inherit ? "Agent model override" : SERVICES[service]}
      className={selectClass}
      value={current}
      onChange={(e) => onChange(choices.find((c) => bindingKey(c.binding) === e.target.value)?.binding ?? { disabled: true })}
    >
      <option value="">{inherit ? "Follow workspace default" : "Disabled / not configured"}</option>
      {value?.connection_id && !choices.some((c) => bindingKey(c.binding) === current) && (
        <option value={current}>Unavailable selection · {value.provider} · r{value.revision}</option>
      )}
      {choices.map((choice) => (
        <option key={bindingKey(choice.binding)} value={bindingKey(choice.binding)}>{choice.label}</option>
      ))}
    </select>
  );
}
