import { useId, useState, type FormEvent } from "react";
import { LoaderCircleIcon, PencilIcon, PlusIcon } from "lucide-react";
import { api } from "@/api";
import type { Agent } from "@/types";
import ProfileSection from "@/components/shared/ProfileSection";
import RichText from "@/components/shared/RichText";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export default function AgentInstructions({ agent, roleInstructions, onUpdated }: {
  agent: Agent;
  roleInstructions?: string;
  onUpdated: (agent: Agent) => void;
}) {
  const id = useId();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const start = () => { setDraft(agent.instructions); setEditing(true); setError(""); setNotice(""); };
  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (busy || draft.trim() === agent.instructions) return;
    setBusy(true);
    setError("");
    try {
      const updated = await api<Agent>("PATCH", `/api/agents/${agent.id}`, { instructions: draft });
      onUpdated(updated);
      setEditing(false);
      setNotice("Instructions saved.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <ProfileSection title="Instructions" action={!editing ? (
      <Button variant="ghost" size="icon-sm" aria-label={`Edit ${agent.name} instructions`} onClick={start}><PencilIcon /></Button>
    ) : undefined}>
      {editing ? (
        <form onSubmit={save} className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <label htmlFor={id} className="min-w-0 text-sm font-medium break-words">How {agent.name} should work</label>
            <div className="flex gap-2">
              <Button type="button" variant="ghost" size="sm" disabled={busy} onClick={() => { setEditing(false); setError(""); }}>Cancel</Button>
              <Button type="submit" size="sm" disabled={busy || draft.trim() === agent.instructions}>
                {busy ? <LoaderCircleIcon className="animate-spin" data-icon="inline-start" /> : null}Save instructions
              </Button>
            </div>
          </div>
          <Textarea id={id} autoFocus rows={14} maxLength={6000} disabled={busy} value={draft}
            onChange={(event) => setDraft(event.target.value)} aria-describedby={`${id}-hint`}
            className="min-h-80 font-mono text-sm leading-relaxed"
            placeholder={`## Responsibilities\nWhat should ${agent.name} take ownership of?\n\n## How to work\nDescribe its approach, preferences, and when to ask for help.\n\n## Voice\nHow should it communicate with teammates?`} />
          <p id={`${id}-hint`} className="text-muted-foreground text-xs leading-relaxed">
            Markdown is supported. Saved instructions guide future responses in every channel this agent joins.
            {roleInstructions ? " These instructions add to its built-in role guidance." : ""}
          </p>
        </form>
      ) : agent.instructions ? (
        <div className="text-sm leading-relaxed"><RichText text={agent.instructions} variant="document" /></div>
      ) : (
        <div className="space-y-3">
          <p className="text-muted-foreground text-sm leading-relaxed">Give {agent.name} guidance on its responsibilities, approach, and how to communicate.</p>
          <Button variant="outline" size="sm" onClick={start}><PlusIcon data-icon="inline-start" />Add instructions</Button>
        </div>
      )}
      {roleInstructions ? <details className="text-muted-foreground mt-4 text-sm">
        <summary className="cursor-pointer text-xs">Built-in role guidance</summary>
        <div className="mt-3 leading-relaxed"><RichText text={roleInstructions} /></div>
      </details> : null}
      {error ? <p role="alert" className="text-destructive mt-3 text-sm">{error}</p> : null}
      {notice ? <p role="status" className="text-muted-foreground mt-3 text-sm">{notice}</p> : null}
    </ProfileSection>
  );
}
