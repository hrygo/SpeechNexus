import { fileURLToPath } from "node:url";

import { test, expect } from "@playwright/test";

import { readDeploymentHeaders, readDeploymentHeadersFromRepository } from "./deployed-headers.mjs";
import { MIXED_CONTRACT_DIGEST, deployedOrigins, mixedOrigins } from "./deployed-servers.mjs";

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
  {
    name: "admin",
    origin: deployedOrigins.admin,
    mixedOrigin: mixedOrigins.admin,
    heading: "运行事实管理",
  },
  {
    name: "workbench",
    origin: deployedOrigins.workbench,
    mixedOrigin: mixedOrigins.workbench,
    heading: "语音任务集中完成",
  },
];

const IDENTITY_PATH = "/v1/nexus/version";
// The identity probe the bundles ship with (#853); every other first-paint request
// stays forbidden, which is what makes the count below a wiring assertion.
const identityRequests = (requests) =>
  requests.filter((url) => new URL(url).pathname === IDENTITY_PATH);

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
      const requests = [];
      page.on("pageerror", (error) => pageErrors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push({ text: message.text(), url: message.location()?.url ?? "" });
        }
      });
      page.on("request", (request) => requests.push(request.url()));

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

      // A fresh page makes exactly one request of its own: the same-version
      // identity probe (#853). This origin has no API attached, so that request
      // fails by design and the browser logs the failed resource; the failure
      // must stay silent in the interface instead of becoming a version warning
      // (#901 P2-3: unknown is not mixed). Every other console error still fails.
      const identityProbe = (entry) => entry.url.endsWith(IDENTITY_PATH);
      const unexpected = consoleErrors.filter((entry) => !identityProbe(entry));
      expect(
        unexpected.map((entry) => entry.text),
        `${application.name} console errors`,
      ).toEqual([]);

      // The probe must actually be wired into the entry: without this count a build
      // with the alert component deleted still passes (`0` filtered errors), which is
      // the hole #901 P2-5 found. Deleting the wiring from `App.tsx` fails here.
      const probes = identityRequests(requests);
      expect(
        probes.length,
        `${application.name} first paint did not ask for its same-version identity`,
      ).toBe(1);
      await expect(
        page.locator("[data-nexus-contract-check]"),
        `${application.name} warned about versions although no API answered`,
      ).toHaveCount(0);
    });

    test("reports the other contract revision this origin answers with", async ({
      context,
      page,
    }) => {
      const deployment = readDeploymentHeadersFromRepository(repositoryRoot);

      await context.route("**/*", (route) => {
        const host = new URL(route.request().url()).host;
        const allowed = [...Object.values(deployedOrigins), ...Object.values(mixedOrigins)].some(
          (origin) => new URL(origin).host === host,
        );
        return allowed ? route.continue() : route.abort("blockedbyclient");
      });

      const pageErrors = [];
      const consoleErrors = [];
      page.on("pageerror", (error) => pageErrors.push(String(error)));
      page.on("console", (message) => {
        if (message.type() === "error") {
          consoleErrors.push(message.text());
        }
      });

      const response = await page.goto(application.mixedOrigin, { waitUntil: "load" });
      expect(response).not.toBeNull();
      expect(response.headers()["content-security-policy"]).toBe(deployment.csp);
      expect(pageErrors).toEqual([]);
      expect(consoleErrors).toEqual([]);

      // Both sides of the disagreement are on screen, and the element carries the
      // attribute automation reads instead of the copy (#901 P2-4).
      const alert = page.locator('[data-nexus-contract-check="mismatch"]');
      await expect(alert, `${application.name} did not report the mixed revisions`).toBeVisible();
      await expect(alert).toContainText("前后端版本不一致");
      // Both sides are quoted, so a reader can compare them without opening a build
      // log: the bundle's baked digest under 前端契约, the API's under 接口契约.
      await expect(alert).toContainText("前端契约");
      await expect(alert).toContainText("接口契约");
      await expect(alert).toContainText(MIXED_CONTRACT_DIGEST.slice(7, 19));

      // The alert is the entry's only alert and it sits above the hero: a reader sees
      // it without scrolling at 1280x560 and 360x800 (measured, not assumed).
      for (const viewport of [
        { width: 1280, height: 560 },
        { width: 360, height: 800 },
      ]) {
        await page.setViewportSize(viewport);
        const box = await alert.boundingBox();
        expect(box, `${application.name} alert has no box at ${viewport.width}px`).not.toBeNull();
        expect(box.y).toBeGreaterThanOrEqual(0);
        expect(
          Math.ceil(box.y + box.height),
          `${application.name} alert is clipped at ${viewport.width}x${viewport.height}`,
        ).toBeLessThanOrEqual(viewport.height);
        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        expect(overflow, `${application.name} alert overflows horizontally`).toBeLessThanOrEqual(0);
      }
    });
  });
}
