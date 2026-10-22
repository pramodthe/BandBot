import assert from "node:assert/strict";
import test from "node:test";
import { importTypeScript } from "./typescript-module.mjs";

const { api, ApiError } = await importTypeScript(new URL("../src/api.ts", import.meta.url));
const { connectionResponse } = await importTypeScript(new URL("../src/components/agents/connection-response.ts", import.meta.url));
const storage = Object.getOwnPropertyDescriptor(globalThis, "localStorage");
Object.defineProperty(globalThis, "localStorage", { value: { getItem: () => null }, configurable: true });
test.after(() => {
  if (storage) Object.defineProperty(globalThis, "localStorage", storage);
  else delete globalThis.localStorage;
});

test("a stale server returning the SPA for an API request gives a recoverable error", async (t) => {
  t.mock.method(globalThis, "fetch", async () => new Response("<html><div id='root'></div></html>", {
    status: 200, headers: { "content-type": "text/html; charset=utf-8" },
  }));
  await assert.rejects(api("GET", "/api/agent-connections"), (error) => error instanceof ApiError
    && error.message.includes("Restart the API and worker"));
});

test("invalid JSON and API errors are reported without exposing response bodies", async (t) => {
  const fetchMock = t.mock.method(globalThis, "fetch", async () => new Response("not-json-secret", {
    headers: { "content-type": "application/json" },
  }));
  await assert.rejects(api("GET", "/api/agent-connections"), /unreadable response/);
  fetchMock.mock.mockImplementation(async () => Response.json({ detail: "Please sign in" }, { status: 401 }));
  await assert.rejects(api("GET", "/api/agent-connections"), (error) => error.status === 401 && error.message === "Please sign in");
});

test("malformed connection responses are rejected before React state can hold them", () => {
  for (const data of [{}, null, "html", { connections: null }, { connections: [], runtimes: {} },
    { connections: [{}], runtimes: { codex: true, claude: true }, codex_chatgpt_sign_in: false }]) {
    assert.throws(() => connectionResponse(data), /Restart the API and worker/);
  }
  const normalized = connectionResponse({ connections: [], runtimes: { codex: true, claude: false }, codex_chatgpt_sign_in: false });
  assert.deepEqual(normalized.runtimes, { codex: true, claude: false, cursor: false, openclaw: false });
  assert.deepEqual(normalized.openclaw_gateways, []);
});
