import { useEffect, useId, useState, type FormEvent } from "react";
import { DownloadIcon } from "lucide-react";
import { api, downloadSkill } from "@/api";
import type { SkillDetail } from "@/types";
import Field from "@/components/shared/Field";
import ModalShell, { actionsClass, formClass } from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

export default function SkillEditor({ skillId, agentId, canManage, copy, onClose, onSaved }: {
  skillId?: string;
  agentId: number;
  canManage: boolean;
  copy?: boolean;
  onClose: () => void;
  onSaved: (message: string) => void;
}) {
  const id = useId();
  const [skill, setSkill] = useState<SkillDetail | null>(null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [instructions, setInstructions] = useState("");
  const [loading, setLoading] = useState(!!skillId);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const readOnly = !canManage || (!!skill?.builtin && !copy);

  useEffect(() => {
    if (!skillId) return;
    let stopped = false;
    api<SkillDetail>("GET", `/api/skills/${skillId}`).then((value) => {
      if (stopped) return;
      setSkill(value);
      setName(copy ? `${value.name.slice(0, 57)}-custom` : value.name);
      setDescription(value.description);
      setInstructions(value.instructions);
    }).catch((cause) => { if (!stopped) setError((cause as Error).message); })
      .finally(() => { if (!stopped) setLoading(false); });
    return () => { stopped = true; };
  }, [skillId, copy]);

  const save = async (event: FormEvent) => {
    event.preventDefault();
    if (loading || busy || readOnly || (skillId && !skill)) return;
    setBusy(true);
    setError("");
    try {
      const body = { name, description, instructions };
      if (skill && !copy) {
        await api("PUT", `/api/skills/${skill.id}`, { ...body, expected_updated_at: skill.updated_at });
      } else {
        await api("POST", "/api/skills", { ...body, agent_id: agentId });
      }
      onSaved(skill && !copy ? "Skill updated for every agent using it." : "Skill created and added to this agent.");
    } catch (cause) {
      setError((cause as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <ModalShell title={loading ? "Loading skill…" : readOnly ? "Skill instructions" : skill && !copy ? "Edit skill" : "Create skill"} wide onClose={() => !busy && onClose()}>
      <form className={formClass} onSubmit={save}>
        <Field id={`${id}-name`} label="Name" hint="lowercase letters, numbers, and hyphens">
          <Input id={`${id}-name`} value={name} onChange={(event) => setName(event.target.value)} placeholder="research-brief" maxLength={64} pattern="[a-z0-9]+(-[a-z0-9]+)*" required readOnly={readOnly} disabled={loading} />
        </Field>
        <Field id={`${id}-description`} label="Description" hint="when should the agent use this skill?">
          <Textarea id={`${id}-description`} value={description} onChange={(event) => setDescription(event.target.value)} rows={2} maxLength={1024} required readOnly={readOnly} disabled={loading} />
        </Field>
        <Field id={`${id}-instructions`} label="Instructions" hint="Markdown supported">
          <Textarea id={`${id}-instructions`} value={instructions} onChange={(event) => setInstructions(event.target.value)} className="min-h-56 font-mono text-sm" rows={10} maxLength={50000} required readOnly={readOnly} disabled={loading} placeholder="Describe the workflow, checks, and expected result…" />
        </Field>
        {skill?.file_count ? <div className="text-muted-foreground text-xs">Supporting files: {Object.keys(skill.files).join(", ")}. Included when you export this skill.</div> : null}
        {skill && !copy && !readOnly ? <p className="text-muted-foreground text-xs">Edits update this reusable skill for all agents using it. New tasks use the updated instructions.</p> : null}
        {copy && skill?.file_count ? <p className="text-muted-foreground text-xs">This copy includes the instructions. Export and import the bundle to preserve supporting files.</p> : null}
        {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
        <div className="flex flex-wrap items-center justify-between gap-3">
          {skill ? <Button type="button" variant="ghost" size="sm" onClick={() => { void downloadSkill(skill.id, skill.name).catch((cause) => setError((cause as Error).message)); }}><DownloadIcon data-icon="inline-start" />Export ZIP</Button> : <span />}
          <div className={actionsClass}>
            <Button type="button" variant="ghost" disabled={busy} onClick={onClose}>{readOnly ? "Close" : "Cancel"}</Button>
            {!readOnly ? <Button disabled={loading || busy || (!!skillId && !skill)}>{busy ? "Saving…" : skill && !copy ? "Save skill" : "Create and add"}</Button> : null}
          </div>
        </div>
      </form>
    </ModalShell>
  );
}
