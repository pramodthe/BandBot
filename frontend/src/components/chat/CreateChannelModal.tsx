import { useId, useState, type FormEvent } from "react";
import { api } from "@/api";
import type { Channel } from "@/types";
import Field from "@/components/shared/Field";
import ModalShell, { actionsClass, formClass } from "@/components/shared/ModalShell";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function CreateChannelModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (id: number) => void;
}) {
  const id = useId();
  const [name, setName] = useState("");
  const [topic, setTopic] = useState("");
  const [error, setError] = useState("");

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    try {
      const channel = await api<Channel>("POST", "/api/channels", { name, topic });
      onCreated(channel.id);
    } catch (err) {
      setError((err as Error).message);
    }
  };

  return (
    <ModalShell title="Create a channel" onClose={onClose}>
      <form className={formClass} onSubmit={submit}>
        <Field id={`${id}-name`} label="Name">
          <Input
            id={`${id}-name`}
            value={name}
            onChange={(e) => setName(e.target.value)}
            placeholder="e.g. launch-plan"
            autoFocus
            required
          />
        </Field>
        <Field id={`${id}-topic`} label="Topic" hint="(optional)">
          <Input
            id={`${id}-topic`}
            value={topic}
            onChange={(e) => setTopic(e.target.value)}
            placeholder="What is this channel about?"
          />
        </Field>
        {error && <div className="text-destructive text-[13.5px]">{error}</div>}
        <div className={actionsClass}>
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button>Create</Button>
        </div>
      </form>
    </ModalShell>
  );
}
