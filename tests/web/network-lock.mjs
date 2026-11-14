/**
 * The previews are the only origins a browser gate may reach.
 *
 * Every other request is aborted with `blockedbyclient`, so a spec can neither
 * pass by silently contacting a real deployment nor leak synthetic data to a
 * third party. The lock lives in one helper because both the accessibility and
 * the behavior gates must hold the same boundary.
 */
const allowedPorts = new Set(["4173", "4174"]);

export async function lockToPreview(context) {
  await context.route("**/*", (route) => {
    const url = new URL(route.request().url());
    if (url.hostname === "127.0.0.1" && allowedPorts.has(url.port)) return route.continue();
    return route.abort("blockedbyclient");
  });
}

/**
 * Prove the lock is actually installed instead of trusting the code path above:
 * a blocked request fails with Chromium's own `ERR_BLOCKED_BY_CLIENT`, which a
 * DNS or connection failure cannot produce.
 */
export async function expectNetworkLocked(page) {
  let failure = null;
  page.on("requestfailed", (request) => {
    if (request.url() === "https://blocked.invalid/preview-lock-probe") {
      failure = request.failure()?.errorText ?? null;
    }
  });
  const outcome = await page.evaluate(async () => {
    try {
      await fetch("https://blocked.invalid/preview-lock-probe");
      return "allowed";
    } catch {
      return "refused";
    }
  });
  if (outcome !== "refused") throw new Error(`Network lock did not refuse an external request`);
  // Playwright's CDP-level abort surfaces as `net::ERR_BLOCKED_BY_CLIENT.Inspector`;
  // a DNS or connection failure never carries this prefix.
  if (!failure?.startsWith("net::ERR_BLOCKED_BY_CLIENT"))
    throw new Error(`Network lock refused without Chromium's blocked-by-client error: ${failure}`);
}
