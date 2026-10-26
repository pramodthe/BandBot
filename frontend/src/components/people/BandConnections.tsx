import { useState } from "react";
import { api } from "@/api";
import type { BandAccount, BandPlatformConnection } from "@/types";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import type { RunAction } from "./PeopleSection";

/** The user's own Band keys: a user key to manage native agents, and an optional existing agent connection. */
export default function BandConnections({ account, platform, busy, run, notify, onAccount, onPlatform }: {
  account: BandAccount | null;
  platform: BandPlatformConnection | null;
  busy: boolean;
  run: RunAction;
  notify: (message: string) => void;
  onAccount: (account: BandAccount) => void;
  onPlatform: (platform: BandPlatformConnection) => void;
}) {
  const [accountKey, setAccountKey] = useState("");
  const [platformKey, setPlatformKey] = useState("");
  return <>
    <section aria-label="Your Band agent management" className="space-y-3 rounded-xl border p-4">
      <h2 className="text-sm font-semibold">Create and manage Band agents</h2>
      <p className="text-muted-foreground text-sm">
        {account?.connected
          ? `Connected as ${account.name || account.handle}. New agents are created in your Band account.`
          : "Connect your own Band user API key to create agents from this app. Other users use their own keys."}
      </p>
      <p className="text-muted-foreground text-xs">Your user account, people, channels, and human messages stay in BandBot.</p>
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            onAccount(await api<BandAccount>("PUT", "/api/band/account", { api_key: accountKey.trim() }));
            setAccountKey("");
            notify("Your Band account is connected. You can now create agents.");
          });
        }}
      >
        <Label htmlFor="management-band-key">Band user API key</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id="management-band-key"
            type="password"
            autoComplete="off"
            placeholder="band_u_…"
            required
            disabled={busy}
            value={accountKey}
            onChange={(event) => setAccountKey(event.target.value)}
            className="min-w-40 flex-1"
          />
          <Button disabled={busy || !accountKey.trim()}>{account?.connected ? "Update management key" : "Connect for agent management"}</Button>
        </div>
        <p className="text-muted-foreground text-xs">
          Create a user key in <a href="https://app.band.ai/users/settings" target="_blank" rel="noreferrer" className="underline">Band Settings → API Keys</a>. It is verified and stored encrypted.
        </p>
      </form>
    </section>
    <section aria-label="Your Band platform connection" className="space-y-3 rounded-xl border p-4">
      <h2 className="text-sm font-semibold">Existing agent connection</h2>
      <p className="text-muted-foreground text-sm">
        {platform?.connected
          ? platform.online === false
            ? platform.error || "Your saved Band connection is unavailable. Update your key to reconnect."
            : "Your Band connection is active. Rooms, teammates and Band collaboration use your saved key."
          : "Optional: connect an existing agent identity with its band_a_ key. This key cannot create new native Band identities."}
      </p>
      <form
        className="space-y-2"
        onSubmit={(event) => {
          event.preventDefault();
          void run(async () => {
            onPlatform(await api<BandPlatformConnection>("PUT", "/api/band/connection", { api_key: platformKey.trim() }));
            setPlatformKey("");
            notify("Your Band connection has been verified and saved.");
          });
        }}
      >
        <Label htmlFor="platform-band-key">{platform?.connected ? "Update Band Agent API key" : "Band Agent API key"}</Label>
        <div className="flex flex-wrap gap-2">
          <Input
            id="platform-band-key"
            type="password"
            autoComplete="off"
            placeholder="band_a_…"
            required
            disabled={busy}
            value={platformKey}
            onChange={(event) => setPlatformKey(event.target.value)}
            className="min-w-40 flex-1"
          />
          <Button disabled={busy || !platformKey.trim()}>{platform?.connected ? "Update connection" : "Connect Band"}</Button>
        </div>
      </form>
    </section>
  </>;
}
