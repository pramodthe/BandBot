import { expect, test, type Page } from "@playwright/test";
import type { Bootstrap, Task } from "../src/types";

function snapshot(online = false): Bootstrap {
  const me = { id: 1, username: "individual", display_name: "Individual user", online: true };
  return {
    me, users: [me], agents: [], roles: [], workspace_roles: {},
    channels: [{ id: 1, name: "general", topic: "", human_ids: [1], agent_ids: [], outside_agents: [],
      band_room_id: null, is_dm: false, dm_agent_id: null, policy: { approvers: "members", computer_control: "members" } }],
    models: [{ id: "test-model", label: "Test model", default: true }],
    band: { configured: online, online, owner_name: null, owner_handle: null, error: null },
    openai_configured: true, llm_configured: true, cursor: 0, invitations: [], contact_requests: [],
  };
}

async function mockWorkspace(page: Page, boot: Bootstrap) {
  await page.route("**/api/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    let body: unknown = [];
    if (path === "/api/bootstrap") body = boot;
    if (path === "/api/status") body = { api: "ok", human_chat: "ok", worker: "ready", providers: [] };
    if (path === "/api/people") body = { contacts: [], requests: [], sent_requests: [] };
    if (path === "/api/plugins") body = { configured: false, plugins: [] };
    await route.fulfill({ json: body });
  });
}

test("a failed optional service names the missing connection", async ({ page }, info) => {
  await mockWorkspace(page, snapshot(true));
  const task: Task = { id: "research-disabled", agent_id: 7, status: "failed", progress: "Needs connection",
    error: "Web research is disabled", attempts: 1, cancel_requested: false, delivery_status: "pending",
    created_at: new Date().toISOString(), updated_at: new Date().toISOString(),
    provider_block: { service: "research", code: "not_configured", connection_id: "", detail: "Web research is disabled" } };
  await page.route("**/api/channels/1/tasks", (route) => route.fulfill({ json: [task] }));
  await page.addInitScript(() => localStorage.setItem("band-slack-token", "offline-test-token"));
  await page.goto("/");
  await page.getByRole("button", { name: "general", exact: true }).click();
  await expect(page.getByText("Web research not configured", { exact: true })).toBeVisible();
  await expect(page.getByText("Needs connection", { exact: true })).toHaveCount(0);
  await page.screenshot({ path: info.outputPath("research-service-status.png"), fullPage: true });
});

test("signup optionally accepts a Band user key and preserves validation feedback", async ({ page }, info) => {
  await mockWorkspace(page, snapshot());
  let attempts = 0;
  let release: () => void = () => {};
  const ready = new Promise<void>((resolve) => { release = resolve; });
  await page.route("**/api/auth/signup", async (route) => {
    attempts += 1;
    const body = route.request().postDataJSON();
    expect(body).toMatchObject({ username: "individual", display_name: "Individual user", password: "test-password" });
    if (body.band_api_key === "band_u_invalid-test") {
      await route.fulfill({ status: 422, json: { detail: "Band could not verify this user key.", field: "band_api_key" } });
    } else {
      expect(body.band_api_key).toBe("band_u_valid-test");
      await ready;
      await route.fulfill({ json: { token: "offline-test-token" } });
    }
  });
  await page.goto("/");
  await page.getByRole("button", { name: "New here? Create an account" }).click();
  await page.getByLabel("Username", { exact: true }).fill("individual");
  await page.getByLabel("Display name").fill("Individual user");
  await page.getByLabel("Password", { exact: true }).fill("test-password");
  const key = page.getByLabel("Band user API key", { exact: true });
  await expect(key).not.toHaveAttribute("required");
  await expect(key).toHaveAttribute("type", "password");
  await page.getByText("How do I get a Band user key?", { exact: true }).click();
  await expect(page.getByRole("link", { name: "Band Settings" })).toHaveAttribute("href", "https://app.band.ai/users/settings");
  await key.fill("sk_wrong-provider-key");
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Enter your Band user API key");
  expect(attempts).toBe(0);
  await key.fill("band_u_invalid-test");
  await page.getByRole("button", { name: "Show Band user API key" }).click();
  await expect(key).toHaveAttribute("type", "text");
  await page.getByRole("button", { name: "Hide Band user API key" }).click();
  await page.getByRole("button", { name: "Create account", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("Band could not verify");
  await expect(key).toHaveAttribute("aria-invalid", "true");
  await expect(key).toBeFocused();
  await expect(page.getByLabel("Username", { exact: true })).toHaveValue("individual");
  await key.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("signup-band-user-key-help.png"), fullPage: true });
  await key.fill("band_u_valid-test");
  await key.press("Enter");
  await expect(page.getByRole("button", { name: "Checking your Band key…" })).toBeDisabled();
  await expect(key).toBeDisabled();
  await expect(page.getByRole("status")).toContainText("Verifying your optional Band connection");
  release();
  await expect(page.getByRole("button", { name: "Add agents", exact: true })).toBeVisible();
  await expect(page.getByText("No agents found.")).toBeVisible();
  expect(attempts).toBe(2);
});

for (const workspaceOnline of [false, true]) {
  test(`individual connects an agent API key (workspace ${workspaceOnline ? "online" : "offline"})`, async ({ page }, info) => {
    await mockWorkspace(page, snapshot(workspaceOnline));
    await page.addInitScript(() => localStorage.setItem("band-slack-token", "offline-test-token"));
    let release: () => void = () => {};
    const ready = new Promise<void>((resolve) => { release = resolve; });
    let created: Record<string, unknown> | null = null;
    await page.route("**/api/agents", async (route) => {
      created = route.request().postDataJSON();
      if (created.band_agent_api_key === "band_u_wrong-key") {
        await route.fulfill({ status: 422, json: { detail: "Use this agent's Band API key (band_a_…), not a human or model-provider key." } });
      } else {
        expect(created.band_agent_api_key).toBe("band_a_valid-test");
        await ready;
        await route.fulfill({ json: { id: 10 } });
      }
    });
    await page.goto("/");
    await page.getByRole("button", { name: "Add agents", exact: true }).click();
    await page.getByRole("button", { name: /Connect Band agent/ }).click();
    await page.getByLabel("Name", { exact: true }).fill("Writer");
    await page.getByLabel("Specialty").fill("Writing clear documents");
    const create = page.getByRole("button", { name: "Connect agent", exact: true });
    await expect(create).toBeDisabled();
    const key = page.getByLabel("Band agent API key");
    await expect(key).toHaveAttribute("type", "password");
    await key.fill("band_u_wrong-key");
    await key.press("Enter");
    await expect(page.getByRole("alert")).toContainText("not a human or model-provider key");
    await expect(page.getByLabel("Name", { exact: true })).toHaveValue("Writer");
    await expect(page.getByLabel("Specialty")).toHaveValue("Writing clear documents");
    await page.getByRole("alert").scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath(`agent-key-error-${workspaceOnline}.png`), fullPage: true });
    await key.fill("band_a_valid-test");
    await key.press("Enter");
    await expect(page.getByRole("button", { name: "Connecting…", exact: true })).toBeDisabled();
    await expect(key).toBeDisabled();
    release();
    await expect(page.getByRole("heading", { name: "Connect Band agent", exact: true })).toHaveCount(0);
    expect(created).toMatchObject({ name: "Writer", specialty: "Writing clear documents", model: "test-model", plugins: [] });
  });
}
