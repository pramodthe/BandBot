import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  // Specs share one API, worker and database and run one at a time in filename order, so
  // accounts persist between them. providers.spec.ts creates the first account (the workspace
  // owner); the zz- prefix runs the full Band management flows after the others.
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  timeout: 180_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: "http://127.0.0.1:18173",
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  projects: [
    { name: "desktop", use: { ...devices["Desktop Chrome"], viewport: { width: 1280, height: 800 } } },
    { name: "compact", use: { ...devices["Desktop Chrome"], viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    command: "node scripts/e2e-serve.mjs",
    url: "http://127.0.0.1:18173",
    reuseExistingServer: false,
    timeout: 120_000,
  },
});
