import { defineConfig } from "@playwright/test";

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
  webServer: [
    {
      command: "pnpm --filter @speechnexus/admin preview --host 127.0.0.1 --port 4173 --strictPort",
      url: "http://127.0.0.1:4173",
      reuseExistingServer: false,
    },
    {
      command:
        "pnpm --filter @speechnexus/workbench preview --host 127.0.0.1 --port 4174 --strictPort",
      url: "http://127.0.0.1:4174",
      reuseExistingServer: false,
    },
  ],
});
