import { prepareWorkerScope, restoreWorkerScope, workerScope } from "./mermaid-worker-scope";
import { renderMermaidSVG } from "beautiful-mermaid";

restoreWorkerScope();

export type RenderRequest = { id: number; code: string };
export type RenderResponse = { id: number; svg: string } | { id: number; error: string };

workerScope.addEventListener("message", ({ data }: MessageEvent<RenderRequest>) => {
  let result: RenderResponse;
  try {
    prepareWorkerScope();
    result = { id: data.id, svg: renderMermaidSVG(data.code, {
      // Zoom renders in a body portal, outside the generated reply's theme scope.
      bg: "var(--background, var(--color-background, #fff))",
      fg: "var(--foreground, var(--color-foreground, #1b1712))",
      muted: "var(--muted-foreground, var(--color-muted-foreground, #76736e))",
      border: "var(--border, var(--color-border, #e9e8e5))",
      accent: "var(--foreground, var(--color-foreground, #1b1712))",
      transparent: true,
    }) };
  } catch (error) {
    result = { id: data.id, error: error instanceof Error ? error.message : "Diagram rendering failed" };
  } finally {
    restoreWorkerScope();
  }
  workerScope.postMessage(result);
});
