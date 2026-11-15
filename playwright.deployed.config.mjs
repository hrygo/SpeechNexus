import { defineConfig } from "@playwright/test";

import { deployedServers } from "./tests/web/deployed-servers.mjs";

/**
 * The deployed-headers gate (#840).
 *
 * Separate from the accessibility and behavior configurations because it drives a
 * different origin set: `vite preview` serves `dist` with no CSP, while this gate
 * serves the same bytes with the headers `deploy/web/nginx.conf` sends. It keeps its
 * own ports (4280/4281 in `tests/web/deployed-servers.mjs`) and output roots so it can
 * run beside the preview gates in CI without sharing a report, a trace or a port.
 */
export default defineConfig({
  testDir: "./tests/web",
  testMatch: "**/*.deployed.spec.mjs",
  fullyParallel: true,
  workers: 2,
  retries: 0,
  timeout: 30_000,
  expect: { timeout: 5_000 },
  outputDir: "test-results/deployed",
  reporter: [
    ["list"],
    ["html", { open: "never", outputFolder: "playwright-report/deployed" }],
    ["json", { outputFile: "test-results/deployed-results.json" }],
  ],
  use: {
    browserName: "chromium",
    headless: true,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
    serviceWorkers: "block",
  },
  webServer: deployedServers,
});
