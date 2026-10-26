import { lazy, Suspense } from "react";
import type { MessageComponent } from "@/types";
import { Chart } from "@/components/assistant-ui/elements/chart";
import { DataTable } from "@/components/assistant-ui/elements/data-table";
import { SpecSheet } from "@/components/assistant-ui/elements/spec-sheet";
import GeneratedForm from "./GeneratedForm";
import GeneratedView from "./GeneratedView";
import LiveAppPreviews from "./LiveAppPreviews";
import GeneratedImage from "./GeneratedImage";
import { cn } from "@/lib/utils";
const AssistantUI = lazy(() => import("./AssistantUI"));
const SandboxedUI = lazy(() => import("./SandboxedUI"));

function Component({ component, channelId, canRespond }: { component: MessageComponent; channelId: number; canRespond: boolean }) {
  switch (component.kind) {
    case "image":
      return <GeneratedImage {...component.props} />;
    case "sandbox":
      return <Suspense fallback={<p role="status">Loading interactive answer…</p>}><SandboxedUI surface={component.props} componentId={component.id} channelId={channelId} canRespond={canRespond} /></Suspense>;
    case "app":
      return null; // App entries share one surface below, including server-only results.
    case "ui":
    case "input":
      return <Suspense fallback={<p role="status" className="text-muted-foreground text-sm">Loading interface…</p>}><AssistantUI component={component} channelId={channelId} canRespond={canRespond} /></Suspense>;
    case "form":
      return <GeneratedForm component={component} channelId={channelId} canRespond={canRespond} />;
    case "view":
      return <GeneratedView view={component.props} />;
    case "table":
      return <DataTable {...component.props} />;
    case "chart":
      return <Chart {...component.props} />;
    case "spec":
      return <SpecSheet {...component.props} />;
  }
}

export default function MessageComponents({ components, channelId, canRespond }: { components: MessageComponent[]; channelId: number; canRespond: boolean }) {
  const apps = components.filter((component) => component.kind === "app");
  return (
    <div className={cn("flex max-w-2xl flex-col gap-2 pt-1.5", components.some((component) => component.kind === "app" || component.kind === "sandbox") && "max-w-none")}>
      {components.map((component) => (
        component.kind === "app" ? component.id === apps[0]?.id ? <LiveAppPreviews key="live-apps" apps={apps} /> : null
          : <Component key={component.id} component={component} channelId={channelId} canRespond={canRespond} />
      ))}
    </div>
  );
}
