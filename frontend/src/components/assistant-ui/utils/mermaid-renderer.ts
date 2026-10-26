import type { RenderRequest, RenderResponse } from "./mermaid-renderer.worker";

type Pending = { resolve: (svg: string) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> };
const pending = new Map<number, Pending>();
const inFlight = new Map<string, Promise<string>>();
let worker: Worker | undefined;
let nextId = 0;

function reset(error: Error) {
  worker?.terminate();
  worker = undefined;
  for (const request of pending.values()) {
    clearTimeout(request.timer);
    request.reject(error);
  }
  pending.clear();
}

function renderer() {
  if (!worker) {
    worker = new Worker(new URL("./mermaid-renderer.worker.ts", import.meta.url), { type: "module" });
    worker.onmessage = ({ data }: MessageEvent<RenderResponse>) => {
      const request = pending.get(data.id);
      if (!request) return;
      pending.delete(data.id);
      clearTimeout(request.timer);
      if ("svg" in data) request.resolve(data.svg);
      else request.reject(new Error(data.error));
    };
    worker.onerror = () => reset(new Error("Diagram renderer could not load"));
    worker.onmessageerror = () => reset(new Error("Diagram renderer returned an invalid result"));
  }
  return worker;
}

export function renderDiagram(code: string): Promise<string> {
  const existing = inFlight.get(code);
  if (existing) return existing;
  const result = new Promise<string>((resolve, reject) => {
    const target = renderer();
    const id = ++nextId;
    const timer = setTimeout(() => reset(new Error("Diagram rendering timed out")), 30_000);
    pending.set(id, { resolve, reject, timer });
    try {
      target.postMessage({ id, code } satisfies RenderRequest);
    } catch (error) {
      reset(error instanceof Error ? error : new Error("Diagram request failed"));
    }
  });
  inFlight.set(code, result);
  const clear = () => { inFlight.delete(code); };
  void result.then(clear, clear);
  return result;
}
