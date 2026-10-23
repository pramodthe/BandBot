import { useId, useState, type FormEvent } from "react";
import { ArrowRightIcon, CheckIcon, MessageCircleIcon, MonitorIcon, UsersIcon } from "lucide-react";
import { api } from "@/api";
import Brand from "@/components/shared/Brand";
import Avatar from "@/components/shared/Avatar";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const id = useId();
  const [mode, setMode] = useState<"login" | "signup" | "recover" | "reset">("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
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
    <div className="bg-background grid h-dvh overflow-y-auto lg:grid-cols-[1.1fr_1fr]">
      <section className="bg-sidebar relative hidden flex-col justify-between border-r border-border p-12 lg:flex xl:p-16">
        <Brand />
        <div className="mx-auto w-full max-w-lg py-12">
          <p className="text-brand-soft-foreground mb-5 text-xs font-semibold uppercase tracking-[0.18em]">Good company. Great work.</p>
          <h1 className="text-5xl leading-[1.12] font-medium tracking-[-0.045em] xl:text-6xl">Big ideas.<br />A team to make<br />them happen<span className="text-primary">.</span></h1>
          <p className="text-muted-foreground mt-6 max-w-sm text-base leading-relaxed">People and AI teammates, sharing context and getting work done together.</p>
          <div className="mt-10 rounded-2xl border border-border bg-background p-5 shadow-xs">
            <div className="mb-5 flex items-center gap-2 text-xs font-medium"><span className="size-1.5 rounded-full bg-primary" />One conversation. A whole team.</div>
            <div className="flex items-center gap-3">
              <Avatar name="You" kind="human" size={36} />
              <div><p className="text-sm font-medium">You set the direction</p><p className="text-muted-foreground text-xs">Share a goal in your channel.</p></div>
            </div>
            <div className="border-border my-2 ml-[18px] h-5 border-l border-dashed" />
            <div className="flex items-center gap-3">
              <div className="flex -space-x-1"><Avatar name="Coordinator" kind="agent" role="master" size={36} /><Avatar name="Researcher" kind="agent" role="researcher" size={36} /><Avatar name="Builder" kind="agent" role="coder" size={36} /></div>
              <div><p className="text-sm font-medium">Your teammates take it forward</p><p className="text-muted-foreground text-xs">Research, build, and collaborate.</p></div>
            </div>
            <div className="border-border my-2 ml-[18px] h-5 border-l border-dashed" />
            <div className="flex items-center gap-3"><span className="bg-brand-soft text-brand-soft-foreground flex size-9 items-center justify-center rounded-xl"><CheckIcon className="size-4" /></span><div><p className="text-sm font-medium">Useful work, ready to review</p><p className="text-muted-foreground text-xs">Follow progress and step in along the way.</p></div></div>
          </div>
        </div>
        <div className="text-muted-foreground flex flex-wrap gap-x-6 gap-y-3 text-xs"><span className="flex items-center gap-2"><MessageCircleIcon className="size-3.5" />Shared conversations</span><span className="flex items-center gap-2"><UsersIcon className="size-3.5" />Persistent teammates</span><span className="flex items-center gap-2"><MonitorIcon className="size-3.5" />Agent computers</span></div>
      </section>
      <main className="flex flex-col px-6 py-8 sm:px-12">
        <Brand className="mb-10 lg:hidden" />
      <form
        className="m-auto flex w-full max-w-[360px] flex-col gap-4 py-8"
        onSubmit={submit}
      >
        <p className="text-primary text-xs font-semibold uppercase tracking-[0.16em]">Your shared workspace</p>
        <h2 className="text-3xl font-semibold tracking-tight">{mode === "login" ? "Welcome back." : mode === "signup" ? "Meet your next team." : mode === "recover" ? "Recover your account." : "Choose a new password."}</h2>
        <p className="text-muted-foreground mb-4 text-sm leading-relaxed">{mode === "login" ? "Pick up the conversation. Keep the work moving." : mode === "signup" ? "Create an account to work alongside your AI teammates." : "Use your verified recovery email and one-time code to get back to your workspace."}</p>
        {mode !== "reset" && (
        <Field id={`${id}-username`} label="Username">
          <Input id={`${id}-username`} className="h-11 rounded-xl" autoComplete="username" placeholder="Your username" value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </Field>
        )}
        {mode === "signup" && (
          <Field id={`${id}-display`} label="Display name">
            <Input
              id={`${id}-display`}
              className="h-11 rounded-xl"
              autoComplete="name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
              placeholder="e.g. Alice Chen"
            />
          </Field>
        )}
        {mode === "reset" && (
          <Field id={`${id}-code`} label="Reset code">
            <Input id={`${id}-code`} value={code} onChange={(e) => setCode(e.target.value)} autoComplete="one-time-code" required />
          </Field>
        )}
        {mode !== "recover" && (
          <Field id={`${id}-password`} label={mode === "reset" ? "New password" : "Password"}>
            <Input id={`${id}-password`} className="h-11 rounded-xl" autoComplete={mode === "login" ? "current-password" : "new-password"} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required minLength={6} />
          </Field>
        )}
        {notice && <div className="text-sm" role="status">{notice}</div>}
        {error && <div className="text-destructive text-[13.5px]" role="alert">{error}</div>}
        <Button className="mt-2 h-11 rounded-xl" disabled={busy}>{mode === "login" ? "Sign in" : mode === "signup" ? "Create account" : mode === "recover" ? "Send reset code" : "Update password"}<ArrowRightIcon /></Button>
        {mode === "login" && (
          <Button type="button" variant="link" size="inline" onClick={() => { setMode("recover"); setError(""); setNotice(""); }}>
            Forgot password?
          </Button>
        )}
        <Button type="button" variant="link" size="inline" onClick={() => { setMode(mode === "signup" ? "login" : mode === "login" ? "signup" : "login"); setError(""); }}>
          {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
        </Button>
      </form>
      <p className="text-muted-foreground mt-8 text-center text-xs">A little more together. A lot more done.</p>
      </main>
    </div>
  );
}
