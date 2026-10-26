import { z } from "zod";

const text = z.string().max(8000);
const short = z.string().max(300);
const number = z.number().finite().min(-1e12).max(1e12);
const count = z.number().int().min(0).max(10000);
const url = z.url().refine((value) => value.startsWith("https://"), "Use an HTTPS URL");
const list = (item) => z.array(item).max(100);
const object = (props) => z.strictObject(props);
const item = (props) => object({ id: short, ...props });
const optional = (schema) => schema.optional();

// Only serializable presentation props belong here. Application callbacks and DOM props
// are deliberately absent; controls that perform work use the host's task APIs.
export const elementSchemas = {
  CodeDiff: object({ filename: short, additions: count, deletions: count, lines: list(object({ kind: z.enum(["context", "added", "removed"]), text })), cycle: optional(count) }),
  FileTree: object({ nodes: list(object({ path: short, name: short, depth: z.number().int().min(0).max(12), kind: z.enum(["folder", "file"]), additions: optional(count), deletions: optional(count) })), totalAdditions: count, totalDeletions: count, visibleCount: optional(count) }),
  LinkPreview: object({ href: url, title: optional(short), description: optional(text), image: optional(url), imageAlt: optional(short), siteName: optional(short), favicon: optional(url), layout: optional(z.enum(["card", "compact"])) }),
  ImageGallery: object({ images: list(item({ src: url, alt: short, caption: optional(text), source: optional(object({ label: optional(short), url: optional(url) })) })), maxVisible: optional(z.number().int().min(1).max(100)) }),
  DocumentReference: object({ title: short, pages: count, anchors: list(object({ page: z.number().int().min(1).max(10000), quote: text })), activePage: optional(count) }),
  ResearchReport: object({ title: short, sections: list(item({ heading: short, state: z.enum(["pending", "writing", "done"]), sources: count, preview: optional(text) })), sourcesRead: count }),
  ComparisonCard: object({ traitLabels: list(short), options: list(item({ name: short, headline: text, traits: list(z.union([text, z.literal(false)])) })), recommendedId: short, reason: text }),
  TodoList: object({ title: optional(short), description: optional(text), items: list(item({ text, status: z.enum(["pending", "active", "done", "failed", "cancelled"]), description: optional(text), reason: optional(text) })), revision: optional(count), maxVisible: optional(count) }),
  FlowGraph: object({ nodes: list(item({ label: short, column: z.number().int().min(0).max(20), row: z.number().int().min(0).max(100), state: z.enum(["done", "active", "pending"]) })), edges: list(object({ from: short, to: short })), visibleCount: optional(count) }),
  NumberTicker: object({ value: number, label: short }),
  AudioPlayer: object({ src: url, title: optional(short), artwork: optional(url), durationMs: optional(count) }),
  VideoPlayer: object({ src: url, title: optional(short), poster: optional(url), ratio: optional(z.enum(["16:9", "4:3", "1:1", "9:16", "auto"])), durationMs: optional(count) }),
  MathBlock: object({ label: optional(short), steps: list(object({ expression: text, note: optional(text) })), visibleSteps: optional(count) }),
  TerminalBlock: object({ command: text, lines: list(text), done: z.boolean(), exitCode: optional(z.number().int()), stderr: optional(list(text)), cwd: optional(short), durationMs: optional(number), truncated: optional(z.boolean()), maxCollapsedLines: optional(count), variant: optional(z.enum(["paper", "ink"])), visibleCount: optional(count) }),
  EventTimeline: object({ events: list(item({ when: z.enum(["past", "now", "future"]), time: short, title: short, detail: optional(text) })), visibleCount: optional(count) }),
  ScoreBreakdown: object({ verdict: short, total: number, outOf: z.number().positive().max(1e12), criteria: list(object({ label: short, score: number, weight: z.number().min(0).max(1e6), note: optional(text) })), visibleCount: optional(count) }),
  AgentPlan: object({ title: optional(short), steps: list(z.union([short, object({ id: optional(short), label: short, description: optional(text) })])), activeIndex: count }),
  RetrievalChunks: object({ query: text, chunks: list(item({ source: short, locator: short, score: z.number().min(0).max(1), text })), searching: z.boolean(), visibleCount: optional(count) }),
  WebSearch: object({ query: text, results: list(object({ title: short, domain: short })), searching: z.boolean(), visibleResults: optional(count), cycle: optional(count) }),
  ActivityGraph: object({ data: list(object({ date: z.iso.date(), count })), start: z.iso.date(), end: z.iso.date(), title: short, total: short }),
};

export const elementModules = {
  CodeDiff: "code-diff", FileTree: "file-tree", LinkPreview: "link-preview",
  ImageGallery: "image-gallery", DocumentReference: "document-reference",
  ResearchReport: "research-report", ComparisonCard: "comparison-card", TodoList: "todo-list",
  FlowGraph: "flow-graph", NumberTicker: "number-ticker", AudioPlayer: "media-player",
  VideoPlayer: "media-player", MathBlock: "math-block",
  TerminalBlock: "terminal-block", EventTimeline: "timeline", ScoreBreakdown: "score-breakdown",
  AgentPlan: "agent-plan", RetrievalChunks: "retrieval-chunks", WebSearch: "web-search", ActivityGraph: "activity-graph",
};
