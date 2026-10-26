import assert from "node:assert/strict";
import { readdir, stat } from "node:fs/promises";

const assets = new URL("../dist/assets/", import.meta.url);
const names = await readdir(assets);
const scripts = await Promise.all(names.filter((name) => name.endsWith(".js"))
  .map(async (name) => ({ name, size: (await stat(new URL(name, assets))).size })));
const pageScripts = scripts.filter(({ name }) => !name.startsWith("mermaid-renderer.worker-"));
for (const { name, size } of pageScripts) {
  assert.ok(size <= 500_000, `${name} exceeds the 500 kB page-script budget (${size} bytes)`);
}
const workers = scripts.filter(({ name }) => name.startsWith("mermaid-renderer.worker-"));
assert.equal(workers.length, 1, "Expected one shared, on-demand diagram worker");
assert.ok(workers[0].size <= 2_000_000, "Diagram worker exceeds its 2 MB budget");
for (const pattern of [/^grammar-cpp-.*\.json$/, /^grammar-emacs-lisp-.*\.json$/, /^onig-.*\.wasm$/]) {
  assert.ok(names.some((name) => pattern.test(name)), `Missing standalone syntax asset: ${pattern}`);
}
console.log(`Bundle budgets passed: largest page script ${Math.max(...pageScripts.map(({ size }) => size))} bytes; ` +
  `on-demand diagram worker ${workers[0].size} bytes.`);
