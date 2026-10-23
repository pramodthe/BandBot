import type { ReactNode } from "react";
import { CircleAlertIcon, TriangleAlertIcon } from "lucide-react";
import type { State } from "@/store";
import { cn } from "@/lib/utils";
import { Alert, AlertDescription } from "@/components/ui/alert";

function Banner({ tone, children }: { tone: "warn" | "error"; children: ReactNode }) {
  const Icon = tone === "error" ? CircleAlertIcon : TriangleAlertIcon;
  return (
    <Alert
      className={cn(
        "border-border rounded-none border-0 border-b px-4 py-2 text-[13.5px]",
        tone === "error" ? "bg-error-bg text-destructive" : "bg-warning-bg text-[#8a6100]",
      )}
    >
      <Icon />
      <AlertDescription className="text-foreground block text-[13.5px]">
        <p>{children}</p>
      </AlertDescription>
    </Alert>
  );
}

const link = "text-mention hover:underline";

export default function SetupBanner({ state, connected }: { state: State; connected: boolean }) {
  if (!connected) {
    return <Banner tone="warn">Reconnecting to the server…</Banner>;
  }
  if (!state.band.configured) {
    return (
      <Banner tone="warn">
        Add your own Band human API key (starts with <code>band_u_</code>) from{" "}
        <a className={link} href="https://app.band.ai/users/settings" target="_blank" rel="noreferrer">
          Band settings
        </a>
        . Open People and connect it. Agents you add use that account.
      </Banner>
    );
  }
  if (!state.band.online) {
    return <Banner tone="error">Band rejected the organization key{state.band.error ? `: ${state.band.error}` : "."}</Banner>;
  }
  if (!state.openaiConfigured) {
    return (
      <Banner tone="warn">
        Ask a workspace admin to connect OpenAI for custom agents and document analysis. Codex and Claude teammates use their own account connections.
      </Banner>
    );
  }
  return null;
}
