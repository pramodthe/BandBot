import { useEffect, useId, useRef, useState, type FormEvent, type ReactNode } from "react";
import { ArrowRightIcon, CheckIcon, LoaderCircleIcon, MessageCircleIcon, MonitorIcon, UsersIcon } from "lucide-react";
import { api, ApiError } from "@/api";
import Brand from "@/components/shared/Brand";
import Avatar from "@/components/shared/Avatar";
import Field from "@/components/shared/Field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import SignupBandConnection from "./SignupBandConnection";

type Mode = "login" | "signup" | "recover" | "reset";

const TITLES: Record<Mode, string> = {
  login: "Welcome back.",
  signup: "Create your account.",
  recover: "Recover your account.",
  reset: "Choose a new password.",
};

const INTROS: Record<Mode, string> = {
  login: "Pick up the conversation. Keep the work moving.",
  signup: "Create your workspace login. Connect Band now or later to create and manage your agents.",
  recover: "Use your verified recovery email and one-time code to get back to your workspace.",
  reset: "Use your verified recovery email and one-time code to get back to your workspace.",
};

const SUBMIT_LABELS: Record<Mode, string> = {
  login: "Sign in",
  signup: "Create account",
  recover: "Send reset code",
  reset: "Update password",
};

const StepConnector = () => <div className="border-border my-2 ml-[18px] h-5 border-l border-dashed" />;

function Step({ title, detail, children }: { title: string; detail: string; children: ReactNode }) {
  return (
    <div className="flex items-center gap-3">
      {children}
      <div>
        <p className="text-sm font-medium">{title}</p>
        <p className="text-muted-foreground text-xs">{detail}</p>
      </div>
    </div>
  );
}

export default function AuthScreen({ onAuthed }: { onAuthed: (token: string) => void }) {
  const id = useId();
  const [mode, setMode] = useState<Mode>("login");
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  const [bandKey, setBandKey] = useState("");
  const [bandError, setBandError] = useState("");
  const bandInput = useRef<HTMLInputElement>(null);
  const [code, setCode] = useState("");
  const [notice, setNotice] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (bandError && !busy) bandInput.current?.focus();
  }, [bandError, busy]);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (busy) return;
    if (mode === "signup" && bandKey.trim() && !/^band_[ua]_/.test(bandKey.trim())) {
      setBandError("Enter your Band user API key beginning with band_u_, or leave this blank and connect later.");
      return;
    }
    setBusy(true);
    setError("");
    setBandError("");
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
      const body = mode === "signup"
        ? { username, display_name: displayName || username, password, band_api_key: bandKey.trim() || undefined }
        : { username, password };
      const result = await api<{ token: string }>("POST", `/api/auth/${mode}`, body);
      setBandKey("");
      onAuthed(result.token);
    } catch (err) {
      if (err instanceof ApiError && err.field === "band_api_key") {
        setBandError(err.message);
      } else setError((err as Error).message);
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
          <h1 className="text-5xl leading-[1.12] font-medium tracking-[-0.045em] xl:text-6xl">
            Big ideas.<br />A team to make<br />them happen<span className="text-primary">.</span>
          </h1>
          <p className="text-muted-foreground mt-6 max-w-sm text-base leading-relaxed">
            People and AI teammates, sharing context and getting work done together.
          </p>
          <div className="mt-10 rounded-2xl border border-border bg-background p-5 shadow-xs">
            <div className="mb-5 flex items-center gap-2 text-xs font-medium">
              <span className="size-1.5 rounded-full bg-primary" />One conversation. A whole team.
            </div>
            <Step title="You set the direction" detail="Share a goal in your channel.">
              <Avatar name="You" kind="human" size={36} />
            </Step>
            <StepConnector />
            <Step title="Your teammates take it forward" detail="Research, build, and collaborate.">
              <div className="flex -space-x-1">
                <Avatar name="Coordinator" kind="agent" role="master" size={36} />
                <Avatar name="Researcher" kind="agent" role="researcher" size={36} />
                <Avatar name="Builder" kind="agent" role="coder" size={36} />
              </div>
            </Step>
            <StepConnector />
            <Step title="Useful work, ready to review" detail="Follow progress and step in along the way.">
              <span className="bg-brand-soft text-brand-soft-foreground flex size-9 items-center justify-center rounded-xl"><CheckIcon className="size-4" /></span>
            </Step>
          </div>
        </div>
        <div className="text-muted-foreground flex flex-wrap gap-x-6 gap-y-3 text-xs">
          <span className="flex items-center gap-2"><MessageCircleIcon className="size-3.5" />Shared conversations</span>
          <span className="flex items-center gap-2"><UsersIcon className="size-3.5" />Persistent teammates</span>
          <span className="flex items-center gap-2"><MonitorIcon className="size-3.5" />Agent computers</span>
        </div>
      </section>
      <main className="flex flex-col px-6 py-8 sm:px-12">
        <Brand className="mb-10 lg:hidden" />
        <form
          className={`m-auto flex w-full flex-col gap-4 py-8 ${mode === "signup" ? "max-w-[420px]" : "max-w-[360px]"}`}
          onSubmit={submit}
        >
          <p className="text-primary text-xs font-semibold uppercase tracking-[0.16em]">Your shared workspace</p>
          <h2 className="text-3xl font-semibold tracking-tight">{TITLES[mode]}</h2>
          <p className="text-muted-foreground mb-2 text-sm leading-relaxed">{INTROS[mode]}</p>
          {mode !== "reset" && (
            <Field id={`${id}-username`} label="Username">
              <Input
                id={`${id}-username`}
                className="h-11 rounded-xl"
                autoComplete="username"
                placeholder="Your username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoFocus
                required
                disabled={busy}
                maxLength={32}
              />
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
                disabled={busy}
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
              <Input
                id={`${id}-password`}
                className="h-11 rounded-xl"
                autoComplete={mode === "login" ? "current-password" : "new-password"}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                required
                minLength={6}
                maxLength={128}
                disabled={busy}
              />
            </Field>
          )}
          {mode === "signup" && (
            <SignupBandConnection
              inputRef={bandInput}
              value={bandKey}
              onChange={(value) => { setBandKey(value); setBandError(""); }}
              disabled={busy}
              error={bandError}
            />
          )}
          {notice && <div className="text-sm" role="status">{notice}</div>}
          {error && <div className="text-destructive text-[13.5px]" role="alert">{error}</div>}
          <Button className="mt-2 h-11 rounded-xl" disabled={busy}>
            {busy ? mode === "signup" && bandKey.trim() ? "Checking your Band key…" : "Please wait…" : SUBMIT_LABELS[mode]}
            {busy ? <LoaderCircleIcon className="animate-spin" /> : <ArrowRightIcon />}
          </Button>
          {busy && mode === "signup" && !!bandKey.trim() && (
            <p role="status" className="text-muted-foreground text-center text-xs">Verifying your optional Band connection.</p>
          )}
          {mode === "login" && (
            <Button type="button" variant="link" size="inline" disabled={busy} onClick={() => { setMode("recover"); setError(""); setNotice(""); }}>
              Forgot password?
            </Button>
          )}
          <Button
            type="button"
            variant="link"
            size="inline"
            disabled={busy}
            onClick={() => {
              setMode(mode === "login" ? "signup" : "login");
              setBandKey("");
              setBandError("");
              setError("");
            }}
          >
            {mode === "login" ? "New here? Create an account" : "Already have an account? Sign in"}
          </Button>
        </form>
        <p className="text-muted-foreground mt-8 text-center text-xs">A little more together. A lot more done.</p>
      </main>
    </div>
  );
}
