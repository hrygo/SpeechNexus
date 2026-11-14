import { defineConfig } from "@playwright/test";

import { previewServers } from "./tests/web/preview-servers.mjs";

// A separate configuration (not the accessibility one) because `testMatch` decides
// which spec files run: one config cannot select both `*.a11y.spec.mjs` and
// `*.behavior.spec.mjs` without either running both suites under one report or
// maintaining a directory split. The preview servers themselves stay shared
// (`tests/web/preview-servers.mjs`), so this file only owns what differs.
export default defineConfig({
  testDir: "./tests/web",
  testMatch: "**/*.behavior.spec.mjs",
  fullyParallel: true,
  workers: 2,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  // Separate output roots so running both gates in one job (CI does) cannot make
  // one gate's report or traces overwrite the other's evidence.
  outputDir: "test-results/behavior",
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report/behavior" }],
    ["json", { outputFile: "test-results/behavior-results.json" }],
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
