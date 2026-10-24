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

export default function SetupBanner({ state, connected }: { state: State; connected: boolean }) {
  if (!connected) {
    return <Banner tone="warn">Reconnecting to the server…</Banner>;
  }
  if (!state.band.configured) {
    return (
      <Banner tone="warn">
        Chat is ready. Add your Band user API key in People when you want to create and manage agents.
      </Banner>
    );
  }
  if (!state.band.online) {
    return <Banner tone="error">Your Band connection is unavailable{state.band.error ? `: ${state.band.error}` : "."}</Banner>;
  }
  if (!state.llmConfigured) {
    return (
      <Banner tone="warn">
        Human chat is ready. Ask a workspace admin to set up Providers & APIs for AI services. Connected teammates retain their own account connections.
      </Banner>
    );
  }
  return null;
}
