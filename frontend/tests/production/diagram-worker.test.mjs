import assert from "node:assert/strict";
import { readFile, readdir } from "node:fs/promises";
import test from "node:test";
import vm from "node:vm";

test("the production worker renders every diagram type without DOM globals", async () => {
  const assets = new URL("../../dist/assets/", import.meta.url);
  const name = (await readdir(assets)).find((file) => /^mermaid-renderer\.worker-.*\.js$/.test(file));
  assert.ok(name, "Build the production diagram worker before this check");
  const source = await readFile(new URL(name, assets), "utf8");
  const replies = [];
  const scope = vm.createContext({
    setTimeout, clearTimeout, performance, atob, btoa, TextEncoder, TextDecoder,
    postMessage: (reply) => replies.push(reply),
    addEventListener(type, listener) {
      assert.equal(type, "message");
      scope.receive = listener;
    },
  });
  // WorkerGlobalScope inherits its `self` getter: deleting an own property cannot hide it.
  vm.runInContext("Object.defineProperty(Object.getPrototypeOf(globalThis), 'self', { get: () => globalThis })", scope);
  // Production workers are ES modules, so assignments to inherited read-only
  // globals must throw instead of silently succeeding in a sloppy VM script.
  vm.runInContext('"use strict";\n' + source, scope, { timeout: 5000 });
  assert.equal(vm.runInContext("self === globalThis", scope), true, "Restore the worker global after ELK loads");
  assert.equal(vm.runInContext("typeof document", scope), "undefined", "Do not leave a fake DOM in the worker");
  const examples = [
    ["sequenceDiagram\nUser->>Agent: Analyze", "Analyze"],
    ["flowchart LR\nA[Upload] --> B[Analyze]", "Upload"],
    ["classDiagram\nclass Agent\nclass Document\nAgent --> Document", "Document"],
    ["erDiagram\nAGENT ||--o{ DOCUMENT : analyzes", "DOCUMENT"],
    ["stateDiagram-v2\n[*] --> Ready\nReady --> Complete", "Complete"],
    ["xychart-beta\nx-axis [Jan, Feb]\ny-axis 0 --> 10\nbar [2, 4]", "Jan"],
  ];
  for (const [id, [code, label]] of examples.entries()) {
    scope.request = { id, code };
    vm.runInContext("receive({ data: request })", scope, { timeout: 5000 });
    const reply = replies.at(-1);
    assert.equal(reply.id, id);
    assert.equal(reply.error, undefined, `${code.split("\n")[0]} failed: ${reply.error}`);
    assert.match(reply.svg, /<svg\b/);
    assert.ok(reply.svg.includes(label), `Missing ${label} in rendered SVG`);
    assert.equal(vm.runInContext("typeof document", scope), "undefined", "Restore the environment after rendering");
    assert.equal(vm.runInContext("Object.hasOwn(globalThis, 'self')", scope), false, "Restore the inherited self getter");
  }
});
