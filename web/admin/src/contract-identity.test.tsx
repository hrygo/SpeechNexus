import { CONTRACT_DIGEST } from "@speechnexus/contracts/contract-digest";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ContractIdentityAlert } from "@speechnexus/shared";

/**
 * The runtime side of #853. The counterexample this must make red is a page that
 * silently reads an artifact produced under another contract revision, so each
 * case here fixes what "not silent" means for one API answer.
 */

const OTHER_DIGEST = `sha256:${"b".repeat(64)}`;

function response(options: {
  status?: number;
  body?: unknown;
  contentType?: string;
  cacheControl?: string | null;
}): Response {
  const status = options.status ?? 200;
  return {
    status,
    ok: status >= 200 && status < 300,
    headers: {
      get(name: string) {
        const key = name.toLowerCase();
        if (key === "content-type") return options.contentType ?? "application/json";
        if (key === "cache-control") {
          return options.cacheControl === undefined ? "no-store" : options.cacheControl;
        }
        return null;
      },
    },
    json: () => {
      if (options.body === undefined) {
        throw new SyntaxError("no body");
      }
      return options.body;
    },
  } as unknown as Response;
}

/**
 * The body of an answer this check never reads must still be released: an open body
 * keeps the request pending for the lifetime of the document, which leaks a connection
 * and makes any "no network activity" wait hang — the real-browser gates use exactly
 * that wait, and a 404 or a static server's HTML was enough to stall them.
 */
it("releases every answer it reads nothing from", async () => {
  for (const answer of [
    { status: 404 },
    { status: 503 },
    { status: 200, contentType: "text/html" },
  ]) {
    const cancel = vi.fn(() => Promise.resolve());
    stubFetch(() => ({ ...response(answer), body: { cancel } }) as unknown as Response);
    const { unmount } = render(<ContractIdentityAlert />);
    await settle();
    expect(cancel).toHaveBeenCalledTimes(1);
    unmount();
  }
});

async function settle() {
  // One macrotask is enough for the fetch promise chain to finish and commit.
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

function stubFetch(
  implementation: (input: RequestInfo | URL, init?: RequestInit) => Promise<Response> | Response,
) {
  const fetchMock = vi.fn(implementation);
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function identityBody(contractDigest: string) {
  return { version: "0.1.0.dev0", contract_digest: contractDigest };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ContractIdentityAlert", () => {
  it("stays silent when the API reports the digest this bundle was built with", async () => {
    stubFetch(() => response({ body: identityBody(CONTRACT_DIGEST) }));

    render(<ContractIdentityAlert />);
    await settle();

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("shows both digests when the API was built from another contract revision", async () => {
    stubFetch(() => response({ body: identityBody(OTHER_DIGEST) }));

    render(<ContractIdentityAlert />);
    await settle();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-nexus-contract-check", "mismatch");
    expect(alert).toHaveTextContent("前后端版本不一致");
    // Plain language: the reader is told what happens and what to ask for.
    expect(alert).toHaveTextContent("不是同一版本构建的");
    expect(alert).toHaveTextContent("一起升级");
    expect(alert).toHaveTextContent(CONTRACT_DIGEST.slice(7, 19));
    expect(alert).toHaveTextContent("bbbbbbbbbbbb");
  });

  it("reports an API that has no version route at all as unconfirmed", async () => {
    // An API built before this check answers the public 404 envelope. That is
    // the half-upgraded deployment the card names: old image, new page.
    stubFetch(() =>
      response({
        status: 404,
        body: {
          error: { code: "not_found", message: "Not Found", request_id: "r", retryable: false },
        },
      }),
    );

    render(<ContractIdentityAlert />);
    await settle();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-nexus-contract-check", "absent");
    expect(alert).toHaveTextContent("可能这次只升级了一半");
  });

  it("reports a body that violates the generated contract as unconfirmed", async () => {
    stubFetch(() => response({ body: { version: "0.1.0.dev0" } }));

    render(<ContractIdentityAlert />);
    await settle();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-nexus-contract-check", "unconfirmed");
    expect(alert).toHaveTextContent("无法确认页面与接口是否同一版本");
  });

  it("reports a cached identity as unconfirmed instead of accepting it", async () => {
    stubFetch(() => response({ body: identityBody(CONTRACT_DIGEST), cacheControl: "max-age=600" }));

    render(<ContractIdentityAlert />);
    await settle();

    expect(screen.getByRole("alert")).toHaveAttribute("data-nexus-contract-check", "unconfirmed");
  });

  it("does not alarm when nothing answered at all", async () => {
    const fetchMock = stubFetch(() => Promise.reject(new TypeError("connection refused")));

    render(<ContractIdentityAlert />);
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("does not treat a static server's HTML answer as a mismatch", async () => {
    stubFetch(() => response({ body: undefined, contentType: "text/html" }));

    render(<ContractIdentityAlert />);
    await settle();

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("asks for the identity exactly once per render", async () => {
    const fetchMock = stubFetch(() => response({ body: identityBody(CONTRACT_DIGEST) }));

    render(<ContractIdentityAlert />);
    await settle();

    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0]?.[0]).toBe("/v1/nexus/version");
  });
});
