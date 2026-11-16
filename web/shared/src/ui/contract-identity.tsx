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
 * sides came from one contract revision. An unequal value, an API without that
 * route, or an answer that does not satisfy the generated contract renders the
 * alert below instead of nothing.
 */

/** The API operation that reports the identity; `/v1/nexus/version` in the contract. */
export const CONTRACT_IDENTITY_PATH = "/v1/nexus/version";

type CheckState =
  | { kind: "checking" }
  | { kind: "aligned" }
  | { kind: "mismatch"; apiDigest: string }
  | { kind: "absent" }
  | { kind: "unconfirmed" }
  | { kind: "unreachable" };

const ALERT_LABEL = {
  mismatch: "前后端版本不一致",
  absent: "无法确认前后端同版",
  unconfirmed: "无法确认前后端同版",
} as const;

type AlertKind = keyof typeof ALERT_LABEL;

const ALERT_COPY: Record<AlertKind, (apiDigest: string) => string> = {
  mismatch: (apiDigest) =>
    "这个页面和它连接的接口不是同一版本构建的。混着用的时候，页面会按旧规则解释新接口的返回，读任务结果和制品就可能读错或读不到。请让部署方把前端和后端一起升级，或者一起回滚。" +
    `前端契约 ${shortDigest(CONTRACT_DIGEST)}，接口契约 ${shortDigest(apiDigest)}。`,
  absent: () =>
    "接口没有提供版本信息，可能这次只升级了一半（页面或接口其中一侧没有跟着换）。这种状态下无法确认页面与接口是否同一版本，读任务结果和制品前请让部署方核对前后端是否同批升级。",
  unconfirmed: () =>
    "接口返回的版本信息不完整，无法确认页面与接口是否同一版本。请让部署方核对前后端是否同批升级或同批回滚。",
};

/** The first six bytes are enough to tell two revisions apart in a sentence. */
function shortDigest(digest: string): string {
  const hex = digest.startsWith("sha256:") ? digest.slice("sha256:".length) : digest;
  return hex.length <= 12 ? hex : `${hex.slice(0, 12)}…`;
}

function isAlertKind(kind: CheckState["kind"]): kind is AlertKind {
  return kind === "mismatch" || kind === "absent" || kind === "unconfirmed";
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
    return { kind: "unreachable" };
  }
  if (response.status === 404) {
    // An API built before this route existed answers the public 404 envelope:
    // that is exactly the half-upgraded deployment the alert exists for.
    await discardBody(response);
    return { kind: "absent" };
  }
  if (!response.ok) {
    await discardBody(response);
    return { kind: "unreachable" };
  }
  const contentType = response.headers.get("content-type")?.split(";", 1)[0]?.trim().toLowerCase();
  if (contentType !== "application/json") {
    // Something else answered, for example a static server's index.html.
    await discardBody(response);
    return { kind: "unreachable" };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // `json()` reads the body, so there is nothing left to release here.
    return { kind: "unconfirmed" };
  }
  try {
    // The generated validator owns the response shape, including the required
    // no-store cache directive: a cached answer must not pass as an identity.
    validateResponse("getVersionIdentity", 200, {
      body,
      headers: { "cache-control": response.headers.get("cache-control") ?? "" },
    });
  } catch {
    return { kind: "unconfirmed" };
  }
  const apiDigest = (body as { contract_digest?: unknown }).contract_digest;
  if (typeof apiDigest !== "string") {
    return { kind: "unconfirmed" };
  }
  return apiDigest === expectedDigest ? { kind: "aligned" } : { kind: "mismatch", apiDigest };
}

/** Run the comparison once on mount; the alert below is the whole visible outcome. */
function useContractIdentity(expectedDigest: string): CheckState {
  const [state, setState] = useState<CheckState>({ kind: "checking" });
  useEffect(() => {
    const controller = new AbortController();
    let mounted = true;
    void compareIdentity(expectedDigest, controller.signal).then((next) => {
      if (mounted) {
        setState(next);
      }
    });
    return () => {
      mounted = false;
      controller.abort();
    };
  }, [expectedDigest]);
  return state;
}

/**
 * Renders nothing while the comparison is running, when the API is unreachable,
 * and — the normal case — when both sides were built from one contract revision.
 */
export function ContractIdentityAlert() {
  const state = useContractIdentity(CONTRACT_DIGEST);
  if (!isAlertKind(state.kind)) {
    return null;
  }
  const detail = state.kind === "mismatch" ? state.apiDigest : "";
  return (
    <div
      role="alert"
      className="boundary-note"
      data-nexus-contract-check={state.kind}
      aria-label="前后端同版本核对"
    >
      <span className="boundary-label">{ALERT_LABEL[state.kind]}</span>
      <p>{ALERT_COPY[state.kind](detail)}</p>
    </div>
  );
}
