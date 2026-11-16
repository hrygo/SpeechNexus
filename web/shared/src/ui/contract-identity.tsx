import { CONTRACT_DIGEST } from "@speechnexus/contracts/contract-digest";
import { validateResponse } from "@speechnexus/contracts/version-http";
import { useEffect, useState } from "react";

/**
 * Runtime "same version" check between a page and the API behind it (#853).
 *
 * A Web bundle is compiled against one revision of `packages/contracts` and a
 * deployment can be upgraded half way: the browser keeps an old bundle while the
 * API moves on, or a rolled-back API answers a fresh page. Neither side used to
 * notice, so a page silently interpreted new responses with old types and read
 * artifacts produced by a contract it was not built for.
 *
 * This guard compares a build fact with a runtime fact. `CONTRACT_DIGEST` is a
 * generated constant baked into the bundle at build time; `GET /v1/nexus/version`
 * reports the digest the running API was generated from. Equal values mean both
 * sides came from one contract revision.
 *
 * Only an unequal pair of digests is reported. Everything that leaves the API side
 * unknown — a route that is not served (404), a proxy or static server's answer, an
 * unreachable host, a timeout, an answer that violates the generated contract — is
 * silence: "cannot confirm" is not "mixed", and a deployment must not show a red
 * banner because a network hop was missing. The manual checklist in
 * `docs/operations/same-version-check.md` covers the unknown cases.
 */

/** The API operation that reports the identity; `/v1/nexus/version` in the contract. */
export const CONTRACT_IDENTITY_PATH = "/v1/nexus/version";

type CheckState =
  | { kind: "checking" }
  | { kind: "aligned" }
  | { kind: "mismatch"; apiDigest: string }
  | { kind: "unknown" };

const ALERT_LABEL = "前后端版本不一致";

const ALERT_COPY =
  "这个页面和它连接的接口不是同一版本构建的。混着用的时候，页面会按旧规则解释新接口的返回，读任务结果和制品就可能读错或读不到。请让部署方把前端和后端一起升级，或者一起回滚。";

/** How long the identity check may take before the API side counts as unknown. */
export const IDENTITY_TIMEOUT_MS = 5000;

/** The first six bytes are enough to tell two revisions apart in a sentence. */
function shortDigest(digest: string): string {
  const hex = digest.startsWith("sha256:") ? digest.slice("sha256:".length) : digest;
  return hex.length <= 12 ? hex : `${hex.slice(0, 12)}…`;
}

/**
 * Release an answer whose body this check never reads.
 *
 * An unread body keeps the request open in the browser: a page that answers 404, 503
 * or a static server's HTML would leave one request pending for the lifetime of the
 * document, which is both a leaked connection and a real gate failure — a browser
 * that waits for "no network activity" (`networkidle`) never settles. Every early
 * answer returns through here first.
 */
async function discardBody(response: Response): Promise<void> {
  try {
    await response.body?.cancel();
  } catch {
    // A body that cannot be cancelled is already released by the browser.
  }
}

/**
 * Read the API identity and compare it with this bundle's digest.
 *
 * `unreachable` stays silent on purpose: a stopped API, an offline page or a
 * static preview server answers no JSON at all, which is not evidence that two
 * versions were mixed. Every answer the API itself gives — a matching digest, a
 * different digest, a missing route, or a body that violates the contract — is
 * judged.
 */
async function compareIdentity(expectedDigest: string, signal: AbortSignal): Promise<CheckState> {
  let response: Response;
  try {
    response = await fetch(CONTRACT_IDENTITY_PATH, {
      headers: { accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch {
    // Offline, a stopped API, a blocked CSP fetch: the API side is unknown, and
    // "unknown" is never reported as a version mismatch.
    return { kind: "unknown" };
  }
  if (!response.ok) {
    // A 404 is what a reverse proxy, a static file server and an API built before
    // this route all answer. None of them identifies the running API's contract, so
    // none of them is evidence that two revisions were mixed (#901 P2-3).
    await discardBody(response);
    return { kind: "unknown" };
  }
  const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/json") {
    // Something else answered, for example a static server's index.html.
    await discardBody(response);
    return { kind: "unknown" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // `json()` reads the body, so there is nothing left to release here.
    return { kind: "unknown" };
  }
  try {
    // The generated validator owns the response shape, including the required
    // no-store cache directive: a cached answer is not an identity.
    validateResponse("getVersionIdentity", 200, {
      body,
      headers: { "cache-control": response.headers.get("cache-control") ?? "" },
    });
  } catch {
    return { kind: "unknown" };
  }
  const apiDigest = (body as { contract_digest?: unknown }).contract_digest;
  if (typeof apiDigest !== "string") {
    return { kind: "unknown" };
  }
  return apiDigest === expectedDigest ? { kind: "aligned" } : { kind: "mismatch", apiDigest };
}

/** Run the comparison once on mount; the alert below is the whole visible outcome. */
function useContractIdentity(expectedDigest: string): CheckState {
  const [state, setState] = useState<CheckState>({ kind: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    let mounted = true;
    // A hanging answer must not stay pending forever: after the budget the API side
    // is unknown (silent), and the request is released.
    const deadline = setTimeout(() => controller.abort(), IDENTITY_TIMEOUT_MS);
    void compareIdentity(expectedDigest, controller.signal).then((next) => {
      if (mounted) {
        setState(next);
      }
    });
    return () => {
      mounted = false;
      clearTimeout(deadline);
      controller.abort();
    };
  }, [expectedDigest]);
  return state;
}

/**
 * Renders nothing while the comparison is running, when the API side is unknown,
 * and — the normal case — when both sides were built from one contract revision.
 */
export function ContractIdentityAlert() {
  const state = useContractIdentity(CONTRACT_DIGEST);
  if (state.kind !== "mismatch") {
    return null;
  }
  return (
    <div
      role="alert"
      className="contract-identity-alert"
      data-nexus-contract-check="mismatch"
      aria-label="前后端版本不一致"
    >
      <span className="contract-identity-alert__label">{ALERT_LABEL}</span>
      <p>
        {ALERT_COPY}
        {`前端契约 ${shortDigest(CONTRACT_DIGEST)}，接口契约 ${shortDigest(state.apiDigest)}。`}
      </p>
    </div>
  );
}
