/**
 * The build-time same-version identity that every Web probe now has to answer (#853).
 *
 * Each `scripts/dev-env/*-probe.mjs` drives a production bundle against a synthetic
 * loopback. Since #853 the bundle asks `GET /v1/nexus/version` once when a page
 * mounts and compares the answer with the contract digest compiled into it: a
 * different digest, a missing route or a body that violates the generated contract
 * raises a visible alert, while an unreachable API stays silent.
 *
 * A probe that means "the page contacted no API" therefore has to say which request
 * it is talking about. Serving this identity keeps that probe's page exactly as it
 * was (the guard stays silent), and counting it separately keeps the old sentence
 * exact instead of quietly absorbed. The values come from generated and declared
 * sources, not from a literal: the digest is the generated constant the bundle was
 * built with, and the version is the one `backend/pyproject.toml` declares — the
 * same values the real API reports.
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { fileURLToPath } from "node:url";

const root = fileURLToPath(new URL("../../", import.meta.url));

/** Where the API reports its identity; `/v1/nexus/version` in the contract. */
export const SAME_VERSION_PATH = "/v1/nexus/version";

const digestSource = readFileSync(
  resolve(root, "packages/contracts/src/contract_digest.generated.ts"),
  "utf8",
);
const digest = /CONTRACT_DIGEST =\s*"(sha256:[0-9a-f]{64})"/.exec(digestSource);
if (!digest) {
  throw new Error(
    "packages/contracts/src/contract_digest.generated.ts carries no CONTRACT_DIGEST; run `make contracts-generate`",
  );
}
/** The contract digest the bundles under test were built with. */
export const CONTRACT_DIGEST = digest[1];

const backendProject = readFileSync(resolve(root, "backend/pyproject.toml"), "utf8");
const version = /^version = "([^"]+)"$/m.exec(backendProject);
if (!version) {
  throw new Error("backend/pyproject.toml declares no version");
}
/** The API version the backend declares, as `GET /v1/nexus/version` reports it. */
export const CONTRACT_VERSION = version[1];

/** Match one request against the identity route, whatever server shape the probe uses. */
export function isSameVersionRequest(method, pathname) {
  return method === "GET" && pathname === SAME_VERSION_PATH;
}

/** The body the API returns; valid against the generated `VersionIdentity` schema. */
export function sameVersionIdentityBody() {
  return { version: CONTRACT_VERSION, contract_digest: CONTRACT_DIGEST };
}

/** The headers the API sends; `Cache-Control: no-store` is part of the contract. */
export function sameVersionIdentityHeaders() {
  return { "Content-Type": "application/json", "Cache-Control": "no-store" };
}

/**
 * Answer a Node `http` response with the identity.
 *
 * `send` exists so a probe cannot half-answer: every probe that serves it sets the
 * same status, content type and cache directive, which is what keeps the bundle's
 * guard silent and the response contract-valid.
 */
export function sendSameVersionIdentity(res) {
  const body = sameVersionIdentityBody();
  res.writeHead(200, sameVersionIdentityHeaders());
  res.end(JSON.stringify(body));
  return body;
}

/**
 * Assert that the only same-version traffic a probe observed used the declared route.
 *
 * The count is deliberately not fixed here: one navigation can mount a page twice
 * (the application redirects or remounts), so pinning the number would fail for a
 * reason unrelated to what the probe proves. What it does prove is that nothing
 * else slipped into the identity lane — a second first-paint request to any other
 * path still lands in the probe's own business counter or its unexpected list.
 */
export function assertOnlySameVersionRequests(identityUrls) {
  assert(identityUrls.length > 0, "the same-version check never reached this harness");
  assert.deepEqual(
    identityUrls.filter((url) => url !== SAME_VERSION_PATH),
    [],
    "every same-version request must be the declared identity route",
  );
}
