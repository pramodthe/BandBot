import { CheckIcon } from "lucide-react";
import Avatar, { agentAvatar, AGENT_AVATARS, AGENT_CHARACTERS } from "@/components/shared/Avatar";
import { cn } from "@/lib/utils";

export default function AgentAvatarPicker({ name, avatar, role, onChange, disabled }: {
  name: string;
  avatar: string | null;
  role?: string;
  onChange: (avatar: string) => void;
  disabled?: boolean;
}) {
  const selected = agentAvatar(avatar, role);
  return (
    <fieldset disabled={disabled} className="flex min-w-0 flex-col items-center gap-4 py-3">
      <legend className="sr-only">Agent character</legend>
      <Avatar name={name || "New agent"} kind="agent" role={role} avatar={avatar} size={96} />
      <div className="grid w-full max-w-sm grid-cols-6 justify-items-center gap-1 sm:gap-2">
        {AGENT_AVATARS.map((choice) => (
          <button key={choice} type="button" aria-label={AGENT_CHARACTERS[choice].label} aria-pressed={selected === choice}
            className={cn("relative flex aspect-square w-full max-w-12 items-center justify-center rounded-xl border border-transparent outline-none transition-colors hover:bg-accent focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50", selected === choice && "border-ring bg-accent")}
            onClick={() => onChange(choice)}>
            <Avatar name={AGENT_CHARACTERS[choice].label} kind="agent" avatar={choice} size={36} />
            {selected === choice ? <span className="bg-foreground text-background absolute -top-1 -right-1 rounded-full p-0.5"><CheckIcon className="size-2.5" /></span> : null}
          </button>
        ))}
      </div>
      <p className="text-muted-foreground text-xs">Choose a character for {name || "your teammate"}.</p>
    </fieldset>
  );
}
