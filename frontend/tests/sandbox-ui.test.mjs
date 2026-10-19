import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

const { UI_SANDBOX, UI_SANDBOX_CSP, sandboxDocument, sandboxProposal } = await importTypeScript(new URL("../src/components/generated/sandbox-document.ts", import.meta.url));

test("interactive source runs behind an opaque-origin policy with no workspace network or form access", () => {
  assert.equal(UI_SANDBOX, "allow-scripts");
  for (const policy of ["connect-src 'none'", "form-action 'none'", "frame-src 'none'", "worker-src 'none'", "object-src 'none'"]) assert.ok(UI_SANDBOX_CSP.includes(policy));
  assert.ok(!/script-src[^;]*\shttps:(?:\s|;)/.test(UI_SANDBOX_CSP));
  const surface = { css: 'p:after{content:"</style>"}', html: "<p>MODEL-CONTENT</p>", jsFunctions: 'function test(){return "</script><script>";}', jsExpressions: "test();" };
  const html = sandboxDocument(surface, "nonce", "https://workspace.example");
  assert.ok(html.indexOf("Content-Security-Policy") < html.indexOf("MODEL-CONTENT"));
  assert.ok(html.indexOf("Object.defineProperty(window") < html.indexOf("MODEL-CONTENT"));
  assert.ok(html.includes("<\\/script>"));
  assert.ok(html.includes('type="importmap"'));
  assert.ok(html.includes("three@0.180.0"));
  assert.ok(html.includes("event.source !== parent"));
  assert.ok(html.includes("parent === window"));
});

test("the bridge admits only bounded follow-up text or credential-free HTTPS links", () => {
  const base = { kind: "band-sandbox:request", nonce: "one", id: "request-1" };
  assert.deepEqual(sandboxProposal({ ...base, method: "sendPrompt", text: " Explain " }, "one"), { id: "request-1", method: "sendPrompt", text: "Explain" });
  assert.deepEqual(sandboxProposal({ ...base, method: "openLink", url: "https://example.com/path" }, "one"), { id: "request-1", method: "openLink", url: "https://example.com/path" });
  for (const bad of [null, [], { ...base, nonce: "wrong", method: "sendPrompt", text: "bad" },
    { ...base, method: "sendPrompt", text: "a".repeat(4001) }, { ...base, method: "sendPrompt", text: " " },
    { ...base, method: "sendPrompt", text: { tool: "send_email" } }, { ...base, method: "execute", tool: "send_email" },
    { ...base, method: "openLink", url: "javascript:alert(1)" }, { ...base, method: "openLink", url: "https://user:pass@example.com/" },
    { ...base, method: "openLink", url: "http://localhost/api" }, { ...base, id: "bad id", method: "sendPrompt", text: "bad" }]) {
    assert.equal(sandboxProposal(bad, "one"), null);
  }
});
