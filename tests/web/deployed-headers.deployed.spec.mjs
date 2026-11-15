import { fileURLToPath } from "node:url";

import { test, expect } from "@playwright/test";

import { readDeploymentHeaders, readDeploymentHeadersFromRepository } from "./deployed-headers.mjs";
import { deployedOrigins } from "./deployed-servers.mjs";

/**
 * First paint of the deployed bundles under the deployed security headers (#840).
 *
 * The accessibility and behavior gates drive `vite preview`, which sends the built
 * bytes with no `Content-Security-Policy`. The image sends `script-src 'self'` from
 * `deploy/web/nginx.conf`, and the runtime used to compile contract validators with
 * `new Function()` on the first parse: every page threw `EvalError` and stayed blank
 * while both preview gates were green.
 *
 * This gate serves the same `dist` bytes with the headers parsed from that
 * configuration, then asserts a real Chromium first paint: non-empty body, the entry
 * heading, no page error and no console error. It covers both applications, because
 * admin and workbench are separate entries with separate bundles.
 *
 * Coverage boundary: this is not a container run. nginx's `always` flag,
 * `add_header` inheritance and override inside `location` blocks, duplicate header
 * handling, `envsubst` substitution of the proxy variables and proxying to a real
 * backend are not exercised here.
 */

const repositoryRoot = fileURLToPath(new URL("../..", import.meta.url));
// The same shape the image sends: a default source plus the strict script source.
const policy = "default-src 'self'; script-src 'self'";

const applications = [
  { name: "admin", origin: deployedOrigins.admin, heading: "运行事实管理" },
  { name: "workbench", origin: deployedOrigins.workbench, heading: "语音任务集中完成" },
];

test.describe("deployment security headers", () => {
  test("nginx.conf declares one policy in every location and does not allow unsafe-eval", () => {
    const headers = readDeploymentHeadersFromRepository(repositoryRoot);
    expect(headers.directives).toBeGreaterThanOrEqual(4);
    expect(headers.csp).toContain("default-src 'self'");
    expect(headers.csp).toContain("script-src 'self'");
    expect(headers.csp).not.toContain("'unsafe-eval'");
    expect(headers.headers["content-security-policy"]).toBe(headers.csp);
  });

  test("a policy that disagrees between locations is refused", () => {
    const configuration = `server {
    add_header Content-Security-Policy "${policy}" always;
    location = /favicon.svg {
        add_header Content-Security-Policy "${policy}" always;
    }
    location ^~ /assets/ {
        add_header Content-Security-Policy "${policy} 'unsafe-eval'" always;
    }
    location / {
        add_header Content-Security-Policy "${policy}" always;
        try_files $uri /index.html;
    }
}`;
    expect(() => readDeploymentHeaders(configuration)).toThrow(
      /inconsistent Content-Security-Policy/,
    );
  });

  test("a location that stops declaring the policy is refused", () => {
    const configuration = `server {
    add_header Content-Security-Policy "${policy}" always;
    location ^~ /assets/ {
        add_header Content-Security-Policy "${policy}" always;
    }
    location / {
        add_header Content-Security-Policy "${policy}" always;
        try_files $uri /index.html;
    }
}`;
    expect(() => readDeploymentHeaders(configuration)).toThrow(
      /declares 3 Content-Security-Policy/,
    );
  });

  test("a policy that allows unsafe-eval is refused instead of certified", () => {
    const weak = `${policy} 'unsafe-eval'`;
    const configuration = `server {
    add_header Content-Security-Policy "${weak}" always;
    location = /favicon.svg {
        add_header Content-Security-Policy "${weak}" always;
    }
    location ^~ /assets/ {
        add_header Content-Security-Policy "${weak}" always;
    }
    location / {
        add_header Content-Security-Policy "${weak}" always;
        try_files $uri /index.html;
    }
}`;
    expect(() => readDeploymentHeaders(configuration)).toThrow(/'unsafe-eval'/);
  });

  test("a configuration without a root location is refused", () => {
    const configuration = `server {
    add_header Content-Security-Policy "${policy}" always;
    location = /favicon.svg {
        add_header Content-Security-Policy "${policy}" always;
    }
    location ^~ /assets/ {
        add_header Content-Security-Policy "${policy}" always;
    }
    location /v1/ {
        add_header Content-Security-Policy "${policy}" always;
        proxy_pass http://upstream;
    }
}`;
    expect(() => readDeploymentHeaders(configuration)).toThrow(/no `location \/` block/);
  });
});

for (const application of applications) {
  test.describe(`${application.name} first paint under the deployed policy`, () => {
    test("renders with no page error or console error", async ({ context, page }) => {
      const deployment = readDeploymentHeadersFromRepository(repositoryRoot);

      // The deployed origins are the only ones this gate may reach.
      await context.route("**/*", (route) => {
        const host = new URL(route.request().url()).host;
        const allowed = Object.values(deployedOrigins).some(
          (origin) => new URL(origin).host === host,
        );
        return allowed ? route.continue() : route.abort("blockedbyclient");
      });

      const pageErrors = [];
      const consoleErrors = [];
      page.on("pageerror", (error) => pageErrors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") consoleErrors.push(message.text());
      });

      const response = await page.goto(application.origin, { waitUntil: "load" });
      expect(response).not.toBeNull();
      expect(response.status()).toBe(200);
      // The policy the browser enforced is the one the image sends, not a test stand-in.
      expect(response.headers()["content-security-policy"]).toBe(deployment.csp);

      // First paint: a blank page has no body text and renders no heading.
      await expect
        .poll(async () => page.evaluate(() => (document.body.innerText ?? "").trim().length), {
          message: `${application.name} rendered no body text under the deployed policy`,
        })
        .toBeGreaterThan(0);
      await expect(
        page.getByRole("heading", { level: 1, name: application.heading }),
        `${application.name} did not render its entry heading`,
      ).toBeVisible();

      // A CSP refusal surfaces as a thrown error and a console error, never as silence.
      expect(pageErrors, `${application.name} page errors`).toEqual([]);
      expect(consoleErrors, `${application.name} console errors`).toEqual([]);
    });
  });
}
