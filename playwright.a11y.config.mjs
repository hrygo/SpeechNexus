import { defineConfig } from "@playwright/test";

import { previewServers } from "./tests/web/preview-servers.mjs";

export default defineConfig({
  testDir: "./tests/web",
  testMatch: "**/*.a11y.spec.mjs",
  fullyParallel: true,
  workers: 2,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  reporter: [
    ["list"],
    ["html", { open: "never" }],
    ["json", { outputFile: "test-results/accessibility-results.json" }],
  ],
  use: {
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    serviceWorkers: "block",
  },
  webServer: previewServers,
});
