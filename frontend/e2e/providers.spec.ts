import { expect, test } from "@playwright/test";

test("admin manages independent local retrieval without an OpenAI key", async ({ page }, info) => {
  await page.goto("/");
  if (info.project.name === "desktop") await page.getByRole("button", { name: "New here? Create an account" }).click();
  await page.getByLabel("Username").fill("provider_settings_admin");
  if (info.project.name === "desktop") await page.getByLabel("Display name").fill("Provider administrator");
  await page.getByLabel("Password").fill("secret123");
  if (info.project.name === "desktop") await page.getByLabel("Band agent API key", { exact: true }).fill("band_a_e2e-provider-admin");
  await page.getByRole("button", { name: info.project.name === "desktop" ? "Create account" : "Sign in", exact: true }).click();
  await expect(page.getByRole("button", { name: /^People/ })).toBeVisible();
  const settings = page.getByRole("button", { name: "Providers & APIs", exact: true });
  await expect(settings).toBeVisible();
  await settings.click();
  await expect(page.getByRole("heading", { name: "Providers & APIs" })).toBeVisible();
  await expect(page.getByLabel("Agent reasoning", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Add connection", exact: true }).click();
  const editor = page.getByRole("region", { name: "Connection editor" });
  await editor.getByLabel("Provider", { exact: true }).selectOption("local");
  await editor.getByLabel("Connection name").fill(`Offline local search ${info.project.name}`);
  await editor.getByRole("button", { name: "Save unverified revision" }).click();
  const card = page.locator("section[aria-label='Provider connections'] > div").filter({ hasText: `Offline local search ${info.project.name}` }).first();
  await card.getByRole("button", { name: "Verify connection", exact: true }).click();
  await expect(card).toContainText("Verified", { timeout: 30000 });
  await page.getByLabel("Document search", { exact: true }).selectOption({ label: `Offline local search ${info.project.name} · r1` });
  await page.getByRole("button", { name: "Preview change" }).click();
  await expect(page.getByText(/unfinished tasks retain their bindings/)).toBeVisible();
  const proposedRetrieval = await page.getByLabel("Document search", { exact: true }).inputValue();
  // Another administrator saves while this preview remains open. A subsequent
  // verification refresh must not silently adopt that administrator's revision.
  const concurrentStatus = await page.evaluate(async () => {
    const headers = { Authorization: `Bearer ${localStorage.getItem("band-slack-token")}`, "Content-Type": "application/json" };
    const saved = await (await fetch("/api/settings/providers", { headers })).json();
    const changed = await fetch("/api/settings/services", { method: "PUT", headers,
      body: JSON.stringify({ expected_version: saved.version, bindings: saved.bindings }) });
    return changed.status;
  });
  expect(concurrentStatus).toBe(200);
  await card.getByRole("button", { name: "Verify connection", exact: true }).click();
  await expect(page.getByText(/Verification queued in the worker/)).toBeVisible();
  const rejected = page.waitForResponse((response) => response.url().endsWith("/api/settings/services") && response.request().method() === "PUT");
  await page.getByRole("button", { name: "Activate reviewed selection" }).click();
  expect((await rejected).status()).toBe(409);
  await expect(page.getByRole("alert")).toContainText("Settings changed");
  await page.getByRole("button", { name: "Reload saved settings" }).click();
  await expect(page.getByLabel("Document search", { exact: true })).not.toHaveValue(proposedRetrieval);
  await page.getByLabel("Document search", { exact: true }).selectOption({ label: `Offline local search ${info.project.name} · r1` });
  await page.getByRole("button", { name: "Preview change" }).click();
  await page.getByRole("button", { name: "Activate reviewed selection" }).click();
  await expect(page.getByText(/Selection submitted/)).toBeVisible();
  await page.getByRole("heading", { name: "Workspace service defaults" }).scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("providers-settings.png"), fullPage: true });
  await page.getByRole("button", { name: "Close", exact: true }).click();
  await expect(page.getByRole("button", { name: /^People/ })).toBeVisible();
});
