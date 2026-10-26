import { lazy, Suspense, useState, type ComponentType } from "react";
import type { GenerativeUILibrary } from "@assistant-ui/generative-ui/react";
import { elementSchemas } from "./element-schemas.mjs";

type Display = ComponentType<Record<string, unknown>>;
const modules = {
  CodeDiff: lazy(() => import("@/components/assistant-ui/elements/code-diff").then((m) => ({ default: m.CodeDiff as unknown as Display }))),
  FileTree: lazy(() => import("@/components/assistant-ui/elements/file-tree").then((m) => ({ default: m.FileTree as unknown as Display }))),
  LinkPreview: lazy(() => import("@/components/assistant-ui/elements/link-preview").then((m) => ({ default: m.LinkPreview as unknown as Display }))),
  ImageGallery: lazy(() => import("@/components/assistant-ui/elements/image-gallery").then((m) => ({ default: m.ImageGallery as unknown as Display }))),
  DocumentReference: lazy(() => import("@/components/assistant-ui/elements/document-reference").then((m) => ({ default: m.DocumentReference as unknown as Display }))),
  ResearchReport: lazy(() => import("@/components/assistant-ui/elements/research-report").then((m) => ({ default: m.ResearchReport as unknown as Display }))),
  ComparisonCard: lazy(() => import("@/components/assistant-ui/elements/comparison-card").then((m) => ({ default: m.ComparisonCard as unknown as Display }))),
  TodoList: lazy(() => import("@/components/assistant-ui/elements/todo-list").then((m) => ({ default: m.TodoList as unknown as Display }))),
  FlowGraph: lazy(() => import("@/components/assistant-ui/elements/flow-graph").then((m) => ({ default: m.FlowGraph as unknown as Display }))),
  NumberTicker: lazy(() => import("@/components/assistant-ui/elements/number-ticker").then((m) => ({ default: m.NumberTicker as unknown as Display }))),
  AudioPlayer: lazy(() => import("@/components/assistant-ui/elements/media-player").then((m) => ({ default: m.AudioPlayer as unknown as Display }))),
  VideoPlayer: lazy(() => import("@/components/assistant-ui/elements/media-player").then((m) => ({ default: m.VideoPlayer as unknown as Display }))),
  MathBlock: lazy(() => import("@/components/assistant-ui/elements/math-block").then((m) => ({ default: m.MathBlock as unknown as Display }))),
  TerminalBlock: lazy(() => import("@/components/assistant-ui/elements/terminal-block").then((m) => ({ default: m.TerminalBlock as unknown as Display }))),
  EventTimeline: lazy(() => import("@/components/assistant-ui/elements/timeline").then((m) => ({ default: m.Timeline as unknown as Display }))),
  ScoreBreakdown: lazy(() => import("@/components/assistant-ui/elements/score-breakdown").then((m) => ({ default: m.ScoreBreakdown as unknown as Display }))),
  AgentPlan: lazy(() => import("@/components/assistant-ui/elements/agent-plan").then((m) => ({ default: m.AgentPlan as unknown as Display }))),
  RetrievalChunks: lazy(() => import("@/components/assistant-ui/elements/retrieval-chunks").then((m) => ({ default: m.RetrievalChunks as unknown as Display }))),
  WebSearch: lazy(() => import("@/components/assistant-ui/elements/web-search").then((m) => ({ default: m.WebSearch as unknown as Display }))),
  ActivityGraph: lazy(() => import("@/components/assistant-ui/elements/activity-graph").then((m) => ({ default: m.ActivityGraph as unknown as Display }))),
};

function DocumentDisplay(props: Record<string, unknown>) {
  const [selection, setSelection] = useState<{ source: unknown; page: number } | null>(null);
  const activePage = selection && selection.source === props.anchors ? selection.page : props.activePage;
  const Component = modules.DocumentReference;
  return <Component {...props} activePage={activePage ?? 1} onJump={(page: number) => setSelection({ source: props.anchors, page })} />;
}

export const elementLibrary: GenerativeUILibrary = Object.fromEntries(Object.entries(modules).map(([name, Component]) => [name, {
  description: `${name}: a native Elements display. Supply verified task data; this is a saved snapshot.`,
  properties: elementSchemas[name]!,
  render: (props: Record<string, unknown>) => {
    const values = { ...props };
    if (values.cycle === undefined) values.cycle = 0;
    if (values.visibleCount === undefined) values.visibleCount = 100;
    if (values.visibleSteps === undefined) values.visibleSteps = 100;
    if (values.visibleResults === undefined) values.visibleResults = 100;
    if (name === "DocumentReference" && values.activePage === undefined) values.activePage = 1;
    // Filter the host defaults as well, so each component receives only its own props.
    const allowed = new Set(Object.keys(elementSchemas[name]!.shape));
    const DisplayComponent = name === "DocumentReference" ? DocumentDisplay : Component;
    return <Suspense fallback={<p role="status">Loading display…</p>}><DisplayComponent {...Object.fromEntries(Object.entries(values).filter(([key]) => allowed.has(key)))} /></Suspense>;
  },
}]));
