import { useId, useState, type FormEvent } from "react";
import { AppWindowIcon, LoaderCircleIcon } from "lucide-react";
import { api } from "@/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

export default function RegisterAppPreview({ taskId }: { taskId: string }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("App preview");
  const [port, setPort] = useState("3000");
  const [path, setPath] = useState("/");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const submit = async (event: FormEvent) => {
    event.preventDefault(); setBusy(true); setError("");
    try {
      await api("POST", `/api/tasks/${taskId}/app-previews`, { title: title.trim(), port: Number(port), path });
      setOpen(false);
    } catch (cause) { setError((cause as Error).message); }
    finally { setBusy(false); }
  };
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild><Button variant="link" size="inline"><AppWindowIcon data-icon="inline-start" />Preview app</Button></DialogTrigger>
    <DialogContent className="sm:max-w-md">
      <DialogHeader><DialogTitle>Preview a running app</DialogTitle><DialogDescription>Attach an app from this agent’s computer to the conversation. The server must already be running.</DialogDescription></DialogHeader>
      <form onSubmit={(event) => void submit(event)} className="flex flex-col gap-4">
        <div className="flex flex-col gap-2"><label className="text-sm font-medium" htmlFor={`${id}-title`}>App name</label><Input id={`${id}-title`} value={title} onChange={(event) => setTitle(event.target.value)} required maxLength={100} /></div>
        <div className="grid grid-cols-[7rem_1fr] gap-3">
          <div className="flex flex-col gap-2"><label className="text-sm font-medium" htmlFor={`${id}-port`}>Server port</label><Input id={`${id}-port`} type="number" min={1024} max={49151} step={1} required value={port} onChange={(event) => setPort(event.target.value)} /></div>
          <div className="flex flex-col gap-2"><label className="text-sm font-medium" htmlFor={`${id}-path`}>Start page</label><Input id={`${id}-path`} required maxLength={2000} placeholder="/" value={path} onChange={(event) => setPath(event.target.value)} /></div>
        </div>
        {error ? <p role="alert" className="text-destructive text-sm">{error}</p> : null}
        <Button type="submit" disabled={busy || !title.trim()}>{busy ? <LoaderCircleIcon className="animate-spin" /> : <AppWindowIcon />}{busy ? "Attaching…" : "Attach live preview"}</Button>
      </form>
    </DialogContent>
  </Dialog>;
}
