import { useId, useState, type FormEvent } from "react";
import { LayersIcon } from "lucide-react";
import { api } from "@/api";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const id = useId();
  const [mode, setMode] = useState<"login" | "signup">("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      const body =
        mode === "signup" ? { username, display_name: displayName || username, password } : { username, password };
      const result = await api<{ token: string }>("POST", `/api/auth/${mode}`, body);
      onAuthed(result.token);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="bg-background grid h-dvh overflow-y-auto px-5 py-10">
      <form
        className="m-auto flex w-full max-w-[360px] flex-col gap-4"
        onSubmit={submit}
      >
        <LayersIcon className="text-primary mb-2 size-8" strokeWidth={2} />
        <h1 className="text-2xl font-semibold tracking-tight">Welcome to BAND_BOTS</h1>
        <p className="text-muted-foreground mb-3 text-sm">A shared space for your team and AI teammates.</p>
        <Field id={`${id}-username`} label="Username">
          <Input id={`${id}-username`} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </Field>
        {mode === "signup" && (
          <Field id={`${id}-display`} label="Display name">
            <Input
              id={`${id}-display`}
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. Alice Chen"
            />
          </Field>
        )}
        <Field id={`${id}-password`} label="Password">
          <Input id={`${id}-password`} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </Field>
        {error && <div className="text-destructive text-[13.5px]">{error}</div>}
        <Button disabled={busy}>{mode === "login" ? "Sign in" : "Create account"}</Button>
        <Button type="button" variant="link" size="inline" onClick={() => setMode(mode === "login" ? "signup" : "login")}>
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </Button>
      </form>
    </div>
  );
}
