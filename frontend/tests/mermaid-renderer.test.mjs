import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

async function fixture(context) {
  const workers = [];
  class FakeWorker {
    requests = [];
    terminated = false;
    constructor(url, options) {
      assert.ok(url.pathname.endsWith("mermaid-renderer.worker.ts"));
      assert.equal(options.type, "module");
      workers.push(this);
    }
    postMessage(request) { this.requests.push(request); }
    terminate() { this.terminated = true; }
    reply(index, result) { this.onmessage({ data: { id: this.requests[index].id, ...result } }); }
  }
  const original = globalThis.Worker;
  globalThis.Worker = FakeWorker;
  context.after(() => { globalThis.Worker = original; });
  return { workers, ...await importTypeScript(new URL("../src/components/assistant-ui/utils/mermaid-renderer.ts", import.meta.url)) };
}

test("concurrent diagrams share one worker and receive their own SVGs", async (context) => {
  const { workers, renderDiagram } = await fixture(context);
  const first = renderDiagram("graph TD\nA-->B");
  const second = renderDiagram("sequenceDiagram\nA->>B: Hello");
  assert.equal(workers.length, 1);
  workers[0].reply(1, { svg: "<svg>second</svg>" });
  workers[0].reply(0, { svg: "<svg>first</svg>" });
  assert.deepEqual(await Promise.all([first, second]), ["<svg>first</svg>", "<svg>second</svg>"]);
});

test("duplicate in-flight renders reuse the same promise", async (context) => {
  const { workers, renderDiagram } = await fixture(context);
  const first = renderDiagram("graph TD\nA-->B");
  assert.equal(renderDiagram("graph TD\nA-->B"), first);
  assert.equal(workers[0].requests.length, 1);
  workers[0].reply(0, { svg: "<svg/>" });
  await first;
});

test("invalid diagrams reject only their own request", async (context) => {
  const { workers, renderDiagram } = await fixture(context);
  const bad = renderDiagram("invalid diagram");
  const good = renderDiagram("graph TD\nA-->B");
  const rejected = assert.rejects(bad, /syntax error/);
  workers[0].reply(0, { error: "syntax error" });
  workers[0].reply(1, { svg: "<svg/>" });
  await rejected;
  assert.equal(await good, "<svg/>");
  assert.equal(workers[0].terminated, false);
});

test("worker failure rejects pending work and the next render reconnects", async (context) => {
  const { workers, renderDiagram } = await fixture(context);
  const first = renderDiagram("first");
  const second = renderDiagram("second");
  const rejected = Promise.all([assert.rejects(first, /could not load/), assert.rejects(second, /could not load/)]);
  workers[0].onerror();
  await rejected;
  assert.equal(workers[0].terminated, true);
  const retry = renderDiagram("first");
  workers[1].reply(0, { svg: "<svg>retried</svg>" });
  assert.equal(await retry, "<svg>retried</svg>");
});

test("a timeout terminates a stuck renderer and releases pending requests", async (context) => {
  const timers = [];
  context.mock.method(globalThis, "setTimeout", (callback) => { timers.push(callback); return timers.length; });
  context.mock.method(globalThis, "clearTimeout", () => {});
  const { workers, renderDiagram } = await fixture(context);
  const result = renderDiagram("stuck render");
  const rejected = assert.rejects(result, /timed out/);
  timers[0]();
  await rejected;
  assert.equal(workers[0].terminated, true);
});

test("postMessage failure closes the worker instead of leaving a pending timeout", async (context) => {
  const { workers, renderDiagram } = await fixture(context);
  const warmup = renderDiagram("warmup");
  workers[0].reply(0, { svg: "<svg/>" });
  await warmup;
  workers[0].postMessage = () => { throw new Error("Cannot send request"); };
  await assert.rejects(renderDiagram("new render"), /Cannot send request/);
  assert.equal(workers[0].terminated, true);
});
