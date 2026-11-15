/**
 * Real deployment headers for the built `dist` bundles, derived from `nginx.conf`.
 *
 * The existing browser gates run `vite preview`, which serves `dist` with no
 * `Content-Security-Policy` at all. The deployed image sets `script-src 'self'`,
 * so a page that needs `'unsafe-eval'` (Ajv compiles validators with
 * `new Function()` at runtime) renders under `vite preview` and blanks in the
 * image: issue #840. This module reads the policy that the image actually sends,
 * so the deployed-headers gate drives the same restriction without Docker.
 *
 * `deploy/web/nginx.conf` repeats the CSP `add_header` in every location that can
 * answer a request. All repeats must agree: a partial edit would leave this gate
 * asserting one value while the image sends another, so a mismatch is a failure
 * rather than a "pick the first one" convenience.
 */
import { readFileSync } from "node:fs";

/** Directives that must appear verbatim in the deployment policy. */
export const REQUIRED_CSP_SOURCES = ["script-src 'self'", "default-src 'self'"];

/**
 * @typedef {object} DeploymentHeaders
 * @property {string} csp        The `Content-Security-Policy` value every location sends.
 * @property {Record<string, string>} headers  Every `add_header` from the root location.
 * @property {number} directives How many CSP directives the configuration repeats.
 */

const ROOT_LOCATION = /location\s+\/\s*\{([\s\S]*?)\n\s{4}\}/;
const ADD_HEADER = /add_header\s+([A-Za-z0-9-]+)\s+"([^"]*)"\s+always;/g;

/**
 * Parse the headers the image sends for the SPA entry and its assets.
 *
 * @param {string} configuration  Full text of `deploy/web/nginx.conf`.
 * @returns {DeploymentHeaders}
 */
export function readDeploymentHeaders(configuration) {
  const directives = [
    ...configuration.matchAll(/add_header\s+Content-Security-Policy\s+"([^"]*)"\s+always;/g),
  ].map((match) => match[1]);
  if (directives.length < 4) {
    throw new Error(
      `deploy/web/nginx.conf declares ${directives.length} Content-Security-Policy ` +
        "add_header directives; the server-level, /, /assets/ and /favicon.svg " +
        "locations must each declare one, otherwise a location could answer without " +
        "the policy and the gate would not observe what the image sends",
    );
  }
  const [csp] = directives;
  for (const [index, directive] of directives.entries()) {
    if (directive !== csp) {
      throw new Error(
        "deploy/web/nginx.conf declares inconsistent Content-Security-Policy headers " +
          `(directive ${index + 1} of ${directives.length} differs from the first): ` +
          `${JSON.stringify(directive)} vs ${JSON.stringify(csp)}`,
      );
    }
  }
  for (const source of REQUIRED_CSP_SOURCES) {
    if (!csp.includes(source)) {
      throw new Error(
        `deploy/web/nginx.conf Content-Security-Policy lost ${JSON.stringify(source)}: ` +
          "a weaker policy would let this gate pass on code the image is supposed to " +
          `refuse; policy is ${JSON.stringify(csp)}`,
      );
    }
  }
  if (csp.includes("'unsafe-eval'")) {
    throw new Error(
      "deploy/web/nginx.conf Content-Security-Policy allows 'unsafe-eval': the " +
        "production build must not need dynamic code evaluation (#840), and this gate " +
        `refuses to certify a page by weakening the policy; policy is ${JSON.stringify(csp)}`,
    );
  }

  const location = configuration.match(ROOT_LOCATION);
  if (!location) {
    throw new Error("deploy/web/nginx.conf has no `location /` block to read headers from");
  }
  /** @type {Record<string, string>} */
  const headers = {};
  for (const match of location[1].matchAll(ADD_HEADER)) {
    headers[match[1].toLowerCase()] = match[2];
  }
  if (headers["content-security-policy"] !== csp) {
    throw new Error(
      "deploy/web/nginx.conf `location /` does not send the policy parsed from the " +
        `whole file: ${JSON.stringify(headers["content-security-policy"])} vs ${JSON.stringify(csp)}`,
    );
  }
  return { csp, headers, directives: directives.length };
}

/** Read and parse `deploy/web/nginx.conf` from the repository root. */
export function readDeploymentHeadersFromRepository(repositoryRoot) {
  return readDeploymentHeaders(readFileSync(`${repositoryRoot}/deploy/web/nginx.conf`, "utf8"));
}
