import type { Page } from "@playwright/test";

export interface SignupDetails {
  username: string;
  displayName: string;
  password?: string;
  /** Optional Band key saved at signup. */
  bandKey?: string;
}

/** Open the signup form and fill it without submitting. */
export async function fillSignup(page: Page, { username, displayName, password = "secret123", bandKey }: SignupDetails) {
  await page.goto("/");
  await page.getByRole("button", { name: "New here? Create an account" }).click();
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Display name").fill(displayName);
  await page.getByLabel("Password", { exact: true }).fill(password);
  if (bandKey) await page.getByLabel("Band user API key", { exact: true }).fill(bandKey);
}

/** Create a local account. */
export async function signup(page: Page, details: SignupDetails) {
  await fillSignup(page, details);
  await page.getByRole("button", { name: "Create account", exact: true }).click();
}

/** Sign in to an account created earlier in the run. */
export async function signIn(page: Page, username: string, password = "secret123") {
  await page.goto("/");
  await page.getByLabel("Username", { exact: true }).fill(username);
  await page.getByLabel("Password", { exact: true }).fill(password);
  await page.getByRole("button", { name: "Sign in", exact: true }).click();
}
