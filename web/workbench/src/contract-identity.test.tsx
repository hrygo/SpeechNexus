import { CONTRACT_DIGEST } from "@speechnexus/contracts/contract-digest";
import { act, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { App } from "./App";

/**
 * The workbench entry wires the shared same-version guard (#853): a page built
 * from one contract revision must say so out loud when the API it reaches was
 * built from another, instead of reading the artifact and showing nothing.
 */

const OTHER_DIGEST = `sha256:${"c".repeat(64)}`;

function jsonResponse(body: unknown): Response {
  return {
    status: 200,
    ok: true,
    headers: {
      get(name: string) {
        const key = name.toLowerCase();
        if (key === "content-type") return "application/json";
        if (key === "cache-control") return "no-store";
        return null;
      },
    },
    json: () => body,
  } as unknown as Response;
}

function stubIdentityFetch(contractDigest: string) {
  const fetchMock = vi.fn(() =>
    Promise.resolve(jsonResponse({ version: "0.1.0.dev0", contract_digest: contractDigest })),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function settle() {
  await act(async () => {
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("workbench same-version guard", () => {
  it("puts the mismatch in front of the user when the API came from another revision", async () => {
    stubIdentityFetch(OTHER_DIGEST);

    render(<App />);
    await settle();

    const alert = screen.getByRole("alert");
    expect(alert).toHaveAttribute("data-nexus-contract-check", "mismatch");
    expect(alert).toHaveTextContent("前后端版本不一致");
    // The artifact this page reads next may be interpreted with stale types; the
    // copy says what the reader should ask for instead of only naming a digest.
    expect(alert).toHaveTextContent("读任务结果和制品就可能读错或读不到");
  });

  it("says nothing when both sides were built from one contract revision", async () => {
    stubIdentityFetch(CONTRACT_DIGEST);

    render(<App />);
    await settle();

    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("says nothing when no identity answer arrives at all", async () => {
    // Offline first paint, a static file server, or an API built before this route:
    // "unknown" is not "mixed" and must not paint a red banner (#901 P2-3).
    const fetchMock = vi.fn(() => Promise.reject(new TypeError("Failed to fetch")));
    vi.stubGlobal("fetch", fetchMock);

    render(<App />);
    await settle();

    expect(fetchMock).toHaveBeenCalled();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("keeps the alert distinct from the standing deployment note", async () => {
    stubIdentityFetch(OTHER_DIGEST);

    render(<App />);
    await settle();

    const alert = screen.getByRole("alert");
    // Two different readings of the same page: a static deployment note is always
    // there, the version alert only when the pair is provably mixed (#901 P2-4).
    expect(alert.className).not.toContain("boundary-note");
    expect(alert).not.toHaveClass("boundary-note");
    expect(document.querySelector("aside.boundary-note")).not.toBeNull();
  });
});
