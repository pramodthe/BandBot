import { useId, useState, type FormEvent } from "react";
import { LayersIcon } from "lucide-react";
import { api } from "@/api";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const id = useId();
  const [mode, setMode] = useState<"login" | "signup" | "recover" | "reset">("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [bandKey, setBandKey] = useState("");
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (mode === "recover") {
        const result = await api<{ message: string }>("POST", "/api/auth/recovery", { username });
        setNotice(result.message);
        setMode("reset");
        return;
      }
      if (mode === "reset") {
        await api("POST", "/api/auth/recovery/reset", { token: code.trim(), password });
        setNotice("Password updated. Sign in with the new password.");
        setPassword("");
        setCode("");
        setMode("login");
        return;
      }
      const body =
        mode === "signup"
          ? { username, display_name: displayName || username, password, band_api_key: bandKey.trim() }
          : { username, password };
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
        {mode !== "reset" && (
          <Field id={`${id}-username`} label="Username">
            <Input id={`${id}-username`} value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
          </Field>
        )}
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
        {mode === "signup" && (
          <Field id={`${id}-band`} label="Band human API key">
            <Input
              id={`${id}-band`}
              type="password"
              autoComplete="off"
              value={bandKey}
              onChange={(e) => setBandKey(e.target.value)}
              placeholder="band_u_…"
              required
              minLength={10}
            />
          </Field>
        )}
        {mode === "signup" && (
          <p className="text-muted-foreground -mt-2 text-xs">Create this key in your own Band account. Agents you add use that account.</p>
        )}
        {mode === "reset" && (
          <Field id={`${id}-code`} label="Reset code">
            <Input id={`${id}-code`} value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" required />
          </Field>
        )}
        {mode !== "recover" && (
          <Field id={`${id}-password`} label={mode === "reset" ? "New password" : "Password"}>
            <Input id={`${id}-password`} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={6} />
          </Field>
        )}
        {notice && <div className="text-sm" role="status">{notice}</div>}
        {error && <div className="text-destructive text-[13.5px]" role="alert">{error}</div>}
        <Button disabled={busy}>{mode === "login" ? "Sign in" : mode === "signup" ? "Create account" : mode === "recover" ? "Send reset code" : "Update password"}</Button>
        {mode === "login" && (
          <Button type="button" variant="link" size="inline" onClick={() => { setMode("recover"); setError(""); setNotice(""); }}>
            Forgot password?
          </Button>
        )}
        <Button type="button" variant="link" size="inline" onClick={() => { setMode(mode === "signup" ? "login" : mode === "login" ? "signup" : "login"); setError(""); }}>
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </Button>
      </form>
    </div>
  );
}
